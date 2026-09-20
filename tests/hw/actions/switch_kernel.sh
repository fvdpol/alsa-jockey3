#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Switch which installed kernel a Raspberry Pi target actually boots.
#
#   switch_kernel.sh <target> [path-to-deb]
#
# THIS SCRIPT ASKS FOR A PASSWORD, AND THAT IS DELIBERATE
# ------------------------------------------------------
# Installing a kernel is root by another name, same reasoning as
# reload_driver.sh for modules: an occasional, deliberate act, not something
# a sudoers rule or jockey3-testctl verb should hand out.
#
# WHY THIS EXISTS
# ----------------
# /etc/kernel/postinst.d/z50-raspi-firmware does not copy the kernel package
# that was just installed -- it recomputes what it thinks is "the latest"
# kernel on every postinst run:
#
#   latest_kernel=$(find /boot -maxdepth 1 -name "vmlinuz-*-rpi-$flavour" \
#                    -print0 | sort -z -V -r | head -z -n1)
#
# It globs every vmlinuz-*-rpi-<flavour> file already in /boot, not just the
# one from the package being installed, and picks the "highest" by
# `sort -V`. Our debug and prod builds of the same commit share one numeric
# kernel version by design -- see targets.yaml's note on LOCALVERSION, which
# is what lets `uname -r` self-identify the target -- so the only difference
# `sort -V` has to compare is the trailing "-alsa-debug" vs "-alsa-prod" text,
# and it falls back to plain lexicographic comparison there: 'p' > 'd', so
# `-alsa-prod-rpi-v8` always wins over `-alsa-debug-rpi-v8`, regardless of
# which one was actually installed most recently or which one apt just ran.
# `apt install ./linux-image-*-alsa-debug*.deb` succeeds and prints nothing
# wrong; the box still boots prod. Found and confirmed on pi4test 2026-09-19
# (sha256sum of /boot/firmware/kernel8.img matched the prod vmlinuz right
# after a clean debug install).
#
# Patching z50-raspi-firmware itself was considered and rejected: it is a
# vendor script, gets silently overwritten by the next `raspi-firmware`
# package upgrade, and its sort-newest-wins behaviour is reasonable for its
# actual job of never auto-downgrading a normal Pi -- it is only wrong for
# us because two of our targets deliberately share a kernel version. So this
# works around it instead of fighting it: never leave two "-rpi-<flavour>"
# kernel packages installed at once. Remove every other one sharing this
# target's raspi-firmware flavour token before installing the one asked for,
# so z50's sort has nothing to prefer.
#
# Verifies the outcome by checksum rather than trusting the postinst output,
# since this whole script exists because that output lies by omission.

set -eu

TARGET=${1:-}
DEB=${2:-}
[ -n "$TARGET" ] || {
	echo "usage: switch_kernel.sh <target> [path-to-deb]" >&2
	echo "  e.g. switch_kernel.sh arm64-debug" >&2
	exit 2
}

LIBDIR=$(dirname "$0")/../lib
CONF=$LIBDIR/machineconf.py
REPO=$(dirname "$0")/../../..

read -r ARCH LOCALVERSION <<<"$(cd "$REPO" && python3 - "$TARGET" <<'PY'
import sys
sys.path.insert(0, "tests/hw")
from lib import yamlio
with open("tests/hw/targets.yaml", encoding="utf-8") as f:
    targets = yamlio.safe_load(f)["targets"]
spec = targets.get(sys.argv[1])
if not spec:
    sys.exit("unknown target '%s'; have: %s" % (sys.argv[1], ", ".join(targets)))
lv = spec.get("localversion")
if not lv:
    sys.exit("target '%s' has no localversion in targets.yaml" % sys.argv[1])
print(spec["arch"], lv)
PY
)"

case "$ARCH" in
arm64) DEBARCH=arm64 ;;
armhf) DEBARCH=armhf ;;
*)
	echo "target '$TARGET' is $ARCH, which raspi-firmware does not package" \
	     "this way -- switch_kernel.sh is for the Pi targets only." >&2
	exit 2
	;;
esac
# dpkg's architecture, not `uname -m`: Raspberry Pi OS ships one armhf
# userland across both armv6l hardware (Pi 1/Zero) and armv7l (Pi 2/3/4
# 32-bit) -- keying off uname -m wrongly refused armhf-prod on pi1test,
# which is armv6l. Debian architecture is what package compatibility
# actually depends on.
[ "$(dpkg --print-architecture)" = "$DEBARCH" ] || {
	echo "this machine is $(dpkg --print-architecture), target '$TARGET'" \
	     "wants $DEBARCH" >&2
	exit 2
}

# --------------------------------------------------------------- get the deb
if [ -z "$DEB" ]; then
	BUILD_HOST=$(python3 "$CONF" get paths.build_host JT_BUILD_HOST alsa-dev)
	echo "looking for a $TARGET kernel package on $BUILD_HOST..."
	remote_path=$(ssh "$BUILD_HOST" \
		"ls -t ~/kbuild/linux-image-*'$LOCALVERSION'-rpi-*_*.deb 2>/dev/null | head -1")
	[ -n "$remote_path" ] || {
		echo "no linux-image-*${LOCALVERSION}-rpi-*_*.deb under ~/kbuild on" \
		     "$BUILD_HOST -- build one with build_kernel.sh $TARGET --package," \
		     "or pass a local .deb path as the second argument." >&2
		exit 2
	}
	DEB=$(mktemp -d)/$(basename "$remote_path")
	echo "fetching $remote_path..."
	scp -q "$BUILD_HOST:$remote_path" "$DEB"
fi
[ -f "$DEB" ] || { echo "no such file: $DEB" >&2; exit 2; }

# Pulls the git describe's short commit hash (the "-g<hash>" component
# dpkg-buildpackage embeds) out of a Debian package Version, since that
# hash -- not the full "7.3.0~rc1-00053-gHASH-7" version string -- is the
# identifier actually used elsewhere for this. Falls back to the caller's
# own default if a build was ever packaged without one (e.g. the stock
# Raspberry Pi OS kernel packages this script also has to deal with).
short_hash() {
	local h
	h=$(echo "$1" | grep -oE 'g[0-9a-f]{7,}' | head -1)
	echo "${h:-$1}"
}

PKGNAME=$(dpkg-deb -f "$DEB" Package)
PKGVERSION=$(dpkg-deb -f "$DEB" Version)
pkg_hash=$(short_hash "$PKGVERSION")
RELEASE=${PKGNAME#linux-image-}
[ "$RELEASE" != "$PKGNAME" ] || {
	echo "$DEB does not look like a linux-image package ($PKGNAME)" >&2
	exit 2
}
case "$RELEASE" in
*"$LOCALVERSION"*) ;;
*)
	echo "warning: $DEB's release '$RELEASE' does not contain this target's" \
	     "LOCALVERSION '$LOCALVERSION' -- proceeding anyway since a" \
	     "package path was given explicitly." >&2
	;;
esac
FLAVOUR=${RELEASE##*-rpi-}
[ "$FLAVOUR" != "$RELEASE" ] || {
	echo "$RELEASE has no '-rpi-<flavour>' suffix -- not a raspi-firmware" \
	     "kernel release, refusing to guess what z50 would glob for it." >&2
	exit 2
}

# Same transform z50-raspi-firmware itself uses to name its destination file
# (kernel_dst= line in /etc/kernel/postinst.d/z50-raspi-firmware): v6 ->
# kernel.img, v7 -> kernel7.img, v8 -> kernel8.img, v8-rt -> kernel8_rt.img,
# 2712 -> kernel_2712.img. Hardcoding kernel8.img instead would silently
# check (or back up) the wrong file -- or one that does not exist at all --
# on anything but a v8 (arm64) target. The matching initramfs filename
# follows the same transform (observed on both v6 and v8, not documented
# anywhere z50 itself owns it -- a separate /etc/initramfs/post-update.d
# hook does that copy).
kernel_dst_name=$(echo "$FLAVOUR" | sed 's/^v//;s/^6//;s/2712/_2712/;s/-/_/;')
KERNEL_DST="/boot/firmware/kernel${kernel_dst_name}.img"
INITRAMFS_DST="/boot/firmware/initramfs${kernel_dst_name}"

# ------------------------------------------------- clear the competing sort
echo "== other -rpi-$FLAVOUR kernels installed =="
# -f='${Package} ${Status}\n' and the $NF check, not just -f='${Package}\n':
# dpkg-query -W lists a package by name regardless of status, so an already
# -removed one (Status "deinstall ok config-files", dpkg -l's "rc") matched
# just as readily as an installed one ("install ok installed") -- every
# rerun after the first re-"removed" the same already-gone packages, apt
# correctly no-op'd each ("not installed, so not removed"), and nothing
# about that was wrong, just noisy and pointless.
mapfile -t OTHERS < <(dpkg-query -W -f='${Package} ${Status}\n' 'linux-image-*' 2>/dev/null \
	| awk '$NF == "installed" {print $1}' \
	| grep -E -- "-rpi-${FLAVOUR}\$" | grep -v -- "^${PKGNAME}\$" || true)
if [ "${#OTHERS[@]}" -eq 0 ]; then
	echo "  none"
else
	printf '  %s\n' "${OTHERS[@]}"
	echo "removing them so z50-raspi-firmware's 'sort -V' has nothing to" \
	     "prefer over $RELEASE..."
	sudo apt remove -y "${OTHERS[@]}"
fi

# ------------------------------------------------------------------ backup
# A Pi has no boot menu and no A/B fallback slot (unlike GRUB, there is
# nowhere else for a bad kernel to leave a working one to boot from) -- and
# the "clear the competing sort" step above just deleted the previous
# release's vmlinuz from the root partition too, on top of the copy in
# /boot/firmware this install is about to overwrite. Left as-is, a kernel
# that fails to boot leaves the standard recovery (pull the SD card, mount
# it on another machine, copy a known-good image over kernel.img) with
# nothing known-good anywhere on the card to copy. One generation of backup,
# overwritten every run, is enough to restore that recovery path without
# relying on anyone remembering to keep their own copy -- a specific
# "known good" snapshot beyond the immediately-previous kernel is still the
# operator's own responsibility, same as always.
if [ -e "$KERNEL_DST" ]; then
	echo "backing up $KERNEL_DST -> $KERNEL_DST.previous..."
	sudo cp -f "$KERNEL_DST" "$KERNEL_DST.previous"
fi
if [ -e "$INITRAMFS_DST" ]; then
	sudo cp -f "$INITRAMFS_DST" "$INITRAMFS_DST.previous"
fi

# ------------------------------------------------------------------ install
echo "installing $PKGNAME ($pkg_hash) (this is the step that needs a password)..."
sudo apt install -y --reinstall "$DEB"

# -------------------------------------------------------------------- verify
have=$(sha256sum "$KERNEL_DST" 2>/dev/null | awk '{print $1}')
want=$(sha256sum "/boot/vmlinuz-$RELEASE" 2>/dev/null | awk '{print $1}')
if [ -z "$want" ]; then
	echo "warning: /boot/vmlinuz-$RELEASE not found to verify against" >&2
elif [ "$have" != "$want" ]; then
	echo "$KERNEL_DST does NOT match vmlinuz-$RELEASE after install -- z50" >&2
	echo "still picked something else. Check for another -rpi-$FLAVOUR" >&2
	echo "kernel this script missed (dpkg -l | grep linux-image)." >&2
	exit 1
else
	echo "verified: $KERNEL_DST matches vmlinuz-$RELEASE"
fi

# uname -r alone cannot tell two builds of the same flavour apart: every
# armhf-prod/arm64-prod/etc build shares one fixed LOCALVERSION by design
# (see targets.yaml), so "currently booted" and "about to run" can print the
# identical release string while being genuinely different kernels. The
# package Version's short commit hash (extracted above) disambiguates them.
current_pkgver=$(dpkg-query -W -f='${Version}' "linux-image-$(uname -r)" 2>/dev/null) || true
if [ -n "$current_pkgver" ]; then
	current_desc=$(short_hash "$current_pkgver")
else
	current_desc="package no longer installed; $(uname -v)"
fi
echo "currently booted: $(uname -r)  ($current_desc)"
echo "reboot now to actually run $RELEASE  ($pkg_hash)."
