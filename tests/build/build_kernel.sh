#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Build a Debian kernel package for one test target.
#
#   ./build_kernel.sh x86_64-debug
#   ./build_kernel.sh arm64-prod --jobs 50
#   ./build_kernel.sh x86_64-debug --clean      # discard the object tree first
#   ./build_kernel.sh x86_64-prod  --prune      # drop objects after packaging
#
# Reads the target's architecture, config and LOCALVERSION from
# tests/hw/targets.yaml, so the thing that gets built and the thing the test
# runner expects cannot disagree.
#
# Where it builds, and why not in ~/sound
# --------------------------------------
# ~/sound is built IN TREE -- build_jockey3.sh uses `make M=` -- and kbuild
# refuses an `O=` build from a source tree that holds in-tree output. The
# project already solved this once for KUnit, with the ~/sound-kunit worktree.
# This does the same: a clean source worktree that is never built in-tree,
# plus one output directory per target.
#
# That keeps ~/sound's 24 GB of in-tree build intact. Reaching for
# `make mrproper` on ~/sound instead would throw it away, along with roughly
# forty minutes, every time a target kernel is built -- which is exactly why
# the project rule is not to.
#
# Environment:
#   KERNEL_SRC     source of truth, never built in (default ~/sound)
#   BUILD_TREE     clean worktree to build from    (default ~/sound-build)
#   BUILD_OUTPUT   root of per-target object dirs  (default ~/kbuild)
#   BUILD_REF      ref to check the worktree out at (default feature/jockey3)
#   DPKG_FLAGS     passed verbatim to dpkg-buildpackage by
#                   scripts/Makefile.package (default -d, skip
#                   dpkg-checkbuilddeps -- these are test kernels, built the
#                   same way every target has been built by hand before this
#                   script existed, and dpkg-checkbuilddeps wants build deps,
#                   e.g. libssl-dev, for host architectures this box may
#                   never have registered as a foreign dpkg arch)

set -eu

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)

KERNEL_SRC=${KERNEL_SRC:-$HOME/sound}
BUILD_TREE=${BUILD_TREE:-$HOME/sound-build}
BUILD_OUTPUT=${BUILD_OUTPUT:-$HOME/kbuild}
BUILD_REF=${BUILD_REF:-feature/jockey3}
DPKG_FLAGS=${DPKG_FLAGS:--d}

TARGET=""
JOBS=$(nproc)
CLEAN=0
PRUNE=0
PACKAGE=1
MIN_FREE_GB=25

usage() { sed -n '3,12p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; }

while [ $# -gt 0 ]; do
	case "$1" in
	--jobs|-j) JOBS=$2; shift 2 ;;
	--clean)   CLEAN=1; shift ;;
	--prune)   PRUNE=1; shift ;;
	--no-pkg)  PACKAGE=0; shift ;;
	--force)   MIN_FREE_GB=0; shift ;;
	--help|-h) usage; exit 0 ;;
	-*) echo "unknown option: $1" >&2; exit 2 ;;
	*)  TARGET=$1; shift ;;
	esac
done

[ -n "$TARGET" ] || { usage; exit 2; }

# ------------------------------------------------------- target definition
# Captured into a variable first: with process substitution the exit status
# would be read's, not python's, so an unknown target would sail through with
# empty values and build something arbitrary.
if ! spec=$(python3 - "$REPO/tests/hw/targets.yaml" "$TARGET" <<-'PY'
	import sys, yaml
	targets = yaml.safe_load(open(sys.argv[1]))["targets"]
	spec = targets.get(sys.argv[2])
	if not spec:
	    sys.exit("unknown target '%s'; have: %s"
	             % (sys.argv[2], ", ".join(targets)))
	for key in ("arch", "config", "localversion"):
	    if not spec.get(key):
	        sys.exit("target '%s' has no %s in targets.yaml"
	                 % (sys.argv[2], key))
	print(spec["arch"], spec["config"], spec["localversion"])
	PY
); then
	echo "$spec" >&2
	exit 2
fi
read -r ARCH CONFIG LOCALVERSION <<<"$spec"

CONFIG_PATH=$REPO/$CONFIG
[ -f "$CONFIG_PATH" ] || {
	echo "no config for $TARGET at $CONFIG_PATH" >&2
	echo "(targets.yaml lists it, but the file has not been created yet)" >&2
	exit 2
}

# Kernel ARCH= names, the Debian architecture for the package, and (for the
# Raspberry Pi targets) the raspi-firmware kernel flavour, from our canonical
# target tokens.
#
# /etc/kernel/postinst.d/z50-raspi-firmware, which copies the kernel image and
# dtbs into /boot/firmware on `dpkg -i`, extracts everything after the first
# literal '-rpi-' in the kernel release and requires it to be EXACTLY 'v8'
# (Pi 4/400/CM4), 'v6' (Pi 1/Zero) etc. -- anything else, including our own
# LOCALVERSION suffix appended after it, and it silently skips the copy with
# "Unsupported kernel version", leaving the new kernel uninstalled with no
# hard error. RPI_LV supplies that exact trailing token; see below for how it
# is applied without colliding with $LOCALVERSION, which already names this
# script's own -alsa-debug/-alsa-prod target tag.
case "$ARCH" in
x86_64) KARCH=x86    ; CROSS=          ; DEBARCH=amd64 ; RPI_LV=        ;;
i386)   KARCH=x86    ; CROSS=          ; DEBARCH=i386  ; RPI_LV=        ;;
arm64)  KARCH=arm64  ; CROSS=aarch64-linux-gnu-     ; DEBARCH=arm64 ; RPI_LV=-rpi-v8 ;;
armhf)  KARCH=arm    ; CROSS=arm-linux-gnueabihf-   ; DEBARCH=armhf ; RPI_LV=-rpi-v6 ;;
*) echo "unsupported architecture '$ARCH'" >&2; exit 2 ;;
esac

if [ -n "$CROSS" ] && ! command -v "${CROSS}gcc" >/dev/null 2>&1; then
	echo "missing cross compiler ${CROSS}gcc" >&2
	exit 3
fi

O=$BUILD_OUTPUT/$TARGET

echo "target       $TARGET  ($ARCH -> ARCH=$KARCH, deb $DEBARCH)"
echo "config       $CONFIG_PATH"
echo "source       $BUILD_TREE  (worktree of $KERNEL_SRC @ $BUILD_REF)"
echo "output       $O"
echo "jobs         $JOBS"
echo

# ------------------------------------------------------------- disk guard
free_gb=$(df -BG --output=avail "$(dirname "$BUILD_OUTPUT")" 2>/dev/null \
	| tail -1 | tr -dc '0-9')
if [ "${free_gb:-0}" -lt "$MIN_FREE_GB" ]; then
	echo "only ${free_gb}G free; a debug kernel object tree needs roughly 25G." >&2
	echo "Free some space, or re-run with --force to try anyway." >&2
	exit 3
fi

# --------------------------------------------------------- source worktree
# Never built in-tree, which is what keeps `O=` working for every target.
#
# It used to be created once at $BUILD_REF and then left alone, with the
# working tree copied over the top on every build. That meant the worktree sat
# at whatever commit it had last been left on -- possibly by build_module.sh --
# while the driver files on top of it came from wherever this repository
# happened to be, committed or not. A kernel built that way is not
# reproducible from anything, and its manifest said only whether this
# repository was clean.
#
# use-committed.sh checks the branch against this repository and moves the
# worktree to that commit, so the package is built from a revision that exists.
echo "== source =="
"$HERE/use-committed.sh" "$KERNEL_SRC" "$BUILD_TREE" "$BUILD_REF"

# ---------------------------------------------------------------- configure
if [ "$CLEAN" -eq 1 ] && [ -d "$O" ]; then
	echo "== discarding $O =="
	rm -rf "$O"
fi
mkdir -p "$O"

echo "== configuring =="
cp "$CONFIG_PATH" "$O/.config"
make -s -C "$BUILD_TREE" O="$O" ARCH="$KARCH" ${CROSS:+CROSS_COMPILE="$CROSS"} \
	olddefconfig

# The config in the repo is the contract. If olddefconfig changed anything
# that matters, say so now rather than discovering it in a test result.
#
# RPI_LV is passed here too, not just at the build step below, so this is
# the actual release the package will carry -- setting it only makes
# scripts/setlocalversion drop the untagged-tree '+' it would otherwise
# append after $LOCALVERSION (CONFIG_LOCALVERSION_AUTO is off in our configs,
# so that '+' is the default), which would break raspi-firmware's exact
# match on the trailing flavour token just as surely as omitting RPI_LV
# altogether.
release=$(make -s -C "$BUILD_TREE" O="$O" ARCH="$KARCH" \
	${CROSS:+CROSS_COMPILE="$CROSS"} ${RPI_LV:+LOCALVERSION="$RPI_LV"} \
	kernelrelease)
echo "   kernel release: $release"
case "$release" in
*"$LOCALVERSION"*) ;;
*)
	echo "   *** release does not contain '$LOCALVERSION'." >&2
	echo "   *** The test runner identifies targets by LOCALVERSION, so this" >&2
	echo "   *** kernel would not be recognized as $TARGET." >&2
	exit 3 ;;
esac

if ! diff -q "$CONFIG_PATH" "$O/.config" >/dev/null; then
	n=$(diff "$CONFIG_PATH" "$O/.config" | grep -c '^[<>]' || true)
	echo "   note: olddefconfig adjusted $n line(s) against the stored config."
	echo "         Review and, if intended, refresh it:"
	echo "         cp $O/.config $CONFIG_PATH"
fi

# -------------------------------------------------------------------- build
echo
echo "== building =="
start=$(date +%s)
if [ "$PACKAGE" -eq 1 ]; then
	make -C "$BUILD_TREE" O="$O" ARCH="$KARCH" \
		${CROSS:+CROSS_COMPILE="$CROSS"} ${RPI_LV:+LOCALVERSION="$RPI_LV"} \
		KBUILD_DEBARCH="$DEBARCH" DPKG_FLAGS="$DPKG_FLAGS" -j"$JOBS" bindeb-pkg
else
	make -C "$BUILD_TREE" O="$O" ARCH="$KARCH" \
		${CROSS:+CROSS_COMPILE="$CROSS"} ${RPI_LV:+LOCALVERSION="$RPI_LV"} \
		-j"$JOBS"
fi
elapsed=$(( $(date +%s) - start ))

echo
echo "built $TARGET ($release) in $((elapsed / 60))m $((elapsed % 60))s"

# Record which driver revision is inside this kernel. Without it, loading the
# module that ships in the package gives a build-id the runner cannot resolve,
# and every result from that kernel says "git revision unknown".
KO=$O/sound/usb/jockey3/snd-reloop-jockey3.ko
if [ -f "$KO" ]; then
	"$HERE/write-manifest.sh" "$KO" "$BUILD_TREE" "$release" || true
fi

# z50-raspi-firmware's postinst unconditionally rsyncs a device-tree overlays
# directory out of the linux-image package for the v8/v6/v7 flavours,
# including `.../overlays/*.dtb*` -- an unquoted glob that /bin/sh, with no
# matches, passes through to rsync as the literal 8-character string
# "*.dtb*" rather than expanding to nothing, so even an EMPTY overlays
# directory still fails with "No such file or directory". Debian's own
# downstream raspi kernel source has an overlay tree to satisfy this;
# mainline does not -- arch/arm64/boot/dts/ here has no overlays/
# subdirectory to install at all -- so the .deb never contains one and
# `dpkg -i` fails on rsync exit 23 instead of installing the kernel.
#
# Fixed by packaging one real, valid, inert overlay: an empty fragment
# targeting "/" that changes nothing even if somehow loaded, compiled with
# the dtc this same build just produced. This driver needs no device-tree
# overlay of its own; this exists purely to give the glob something to
# match.
#
# A second, unrelated z50 gap gets fixed in the same pass: it only rsyncs
# `bcm27*.dtb` into /boot/firmware, which is Raspberry Pi's own downstream
# kernel's legacy per-SoC naming (bcm2708/2709/2710/2711/2712). A mainline
# build's board dtbs are named after the actual SoC instead (bcm2835 for
# the v6 flavour's boards, e.g. bcm2835-rpi-b-plus.dtb for a Pi 1B+), so
# they fall outside that glob and are silently never installed -- the
# board then boots the new kernel against whatever dtb was already in
# /boot/firmware from a previous install. First hit and diagnosed on
# pi1test, 2026-08-22 (see implementation_plan.md / session notes).
#
# Fixed by appending a small step to the package's own DEBIAN/postinst,
# after dpkg's generated `run-parts /etc/kernel/postinst.d` call, that
# copies this kernel's own bcm28*.dtb alongside whatever z50 installed.
# Deliberately NOT a file dropped under /etc/kernel/postinst.d/: that
# directory is unversioned, so a fixed filename there would collide the
# next time a same-flavour kernel package is installed ("trying to
# overwrite ... also in package ..."), where every path a kernel-image
# package DOES own is namespaced by $release for exactly this reason.
# Appending to the maintainer script instead can't collide, since it is
# not a filesystem path. Deliberately does NOT touch the legacy bcm27*
# names or config.txt: overwriting bcm2708-rpi-b-plus.dtb with mainline
# content would silently hand a mainline devicetree to a downstream/rpt
# kernel installed later, and board selection belongs to the rig, not the
# package.
if [ "$PACKAGE" -eq 1 ] && [ -n "$RPI_LV" ]; then
	image_deb=$(find "$BUILD_OUTPUT" -maxdepth 1 \
		-name "linux-image-${release}_*.deb" -newermt "@$start" | head -1)
	dtc=$O/scripts/dtc/dtc
	if [ -n "$image_deb" ]; then
		tmp=$(mktemp -d)
		dpkg-deb -R "$image_deb" "$tmp"
		changed=0

		ov_dir="usr/lib/linux-image-${release}/overlays"
		if [ -x "$dtc" ] && [ ! -d "$tmp/$ov_dir" ]; then
			mkdir -p "$tmp/$ov_dir"
			cat > "$tmp/$ov_dir/README" <<-EOF
				No Raspberry Pi downstream device-tree overlays here: this is
				a mainline kernel build, which has no overlay tree to
				package. placeholder.dtbo is an empty fragment targeting "/"
				that changes nothing -- it exists only because
				z50-raspi-firmware's postinst unconditionally globs for
				*.dtb* in this directory and fails if nothing matches.
			EOF
			placeholder=$(mktemp -d)
			cat > "$placeholder/placeholder.dts" <<-'EOF'
				/dts-v1/;
				/plugin/;
				/ {
					compatible = "brcm,bcm2711";
					fragment@0 {
						target-path = "/";
						__overlay__ { };
					};
				};
			EOF
			"$dtc" -@ -I dts -O dtb \
				-o "$tmp/$ov_dir/placeholder.dtbo" \
				"$placeholder/placeholder.dts"
			rm -rf "$placeholder"
			changed=1
			echo "   added a placeholder $ov_dir/ to $(basename "$image_deb")" \
				"(raspi-firmware requires *.dtb* to match there)"
		fi

		# Mirrors z50's own flavour switch (v8/v8-rt/2712 -> .../broadcom,
		# v6/v7 -> flat) so the copy looks in the same place z50 does.
		case "$RPI_LV" in
		-rpi-v8|-rpi-v8-rt|-rpi-2712)
			dtb_src_dir="usr/lib/linux-image-${release}/broadcom" ;;
		*)
			dtb_src_dir="usr/lib/linux-image-${release}" ;;
		esac
		postinst=$tmp/DEBIAN/postinst
		if [ -f "$postinst" ] && [ -d "$tmp/$dtb_src_dir" ] \
			&& ! grep -q "jockey3: copy this kernel's own bcm28\*.dtb" "$postinst"; then
			# Insert before the script's own trailing `exit 0`, so this runs
			# after run-parts (and thus after z50) but is still covered by
			# the script's `set -e`.
			tmp_postinst=$(mktemp)
			sed '$ d' "$postinst" > "$tmp_postinst"
			cat >> "$tmp_postinst" <<-EOF
				# jockey3: copy this kernel's own bcm28*.dtb into /boot/firmware --
				# z50-raspi-firmware only rsyncs bcm27*.dtb, which a mainline build's
				# board dtbs (bcm28*.dtb) never match. Never touches the legacy
				# bcm27* names or config.txt; quiet no-op off a Pi / without
				# /boot/firmware mounted, same as z50's own guard.
				if [ -d /boot/firmware ]; then
					for f in /$dtb_src_dir/bcm28*.dtb; do
						[ -e "\$f" ] || continue
						cp -f "\$f" /boot/firmware/
					done
					sync -f /boot/firmware 2>/dev/null || true
				fi
			EOF
			echo "exit 0" >> "$tmp_postinst"
			cp "$tmp_postinst" "$postinst"
			rm -f "$tmp_postinst"
			changed=1
			echo "   appended a bcm28*.dtb copy step to $(basename "$image_deb")'s postinst" \
				"(raspi-firmware only installs bcm27*.dtb)"
		fi

		[ "$changed" -eq 1 ] && dpkg-deb --root-owner-group -b "$tmp" "$image_deb" >/dev/null
		rm -rf "$tmp"
	fi
fi

if [ "$PACKAGE" -eq 1 ]; then
	echo
	echo "packages:"
	find "$BUILD_OUTPUT" "$(dirname "$O")" -maxdepth 1 -name "*.deb" \
		-newermt "@$start" -printf '  %p  (%s bytes)\n' 2>/dev/null \
		| sort -u
	echo
	echo "install on the target machine with:"
	echo "  sudo dpkg -i linux-image-${release}_*.deb"
fi

# Each target's object tree is ~10-22 GB, mostly uncompressed DWARF. Pruning
# keeps the packages and the config and discards the objects, so several
# targets can be built in sequence on a disk that could not hold them all at
# once. The cost is that the next build of this target starts from scratch.
if [ "$PRUNE" -eq 1 ]; then
	before=$(du -sBG "$O" 2>/dev/null | cut -f1)
	echo
	echo "== pruning object tree (was ${before:-?}) =="
	make -s -C "$BUILD_TREE" O="$O" ARCH="$KARCH" \
		${CROSS:+CROSS_COMPILE="$CROSS"} clean
	echo "   now $(du -sh "$O" 2>/dev/null | cut -f1); .config kept"
fi
