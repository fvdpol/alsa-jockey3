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

set -eu

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)

KERNEL_SRC=${KERNEL_SRC:-$HOME/sound}
BUILD_TREE=${BUILD_TREE:-$HOME/sound-build}
BUILD_OUTPUT=${BUILD_OUTPUT:-$HOME/kbuild}
BUILD_REF=${BUILD_REF:-feature/jockey3}

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

# Kernel ARCH= names and the Debian architecture for the package, from our
# canonical target tokens.
case "$ARCH" in
x86_64) KARCH=x86    ; CROSS=          ; DEBARCH=amd64 ;;
i386)   KARCH=x86    ; CROSS=          ; DEBARCH=i386  ;;
arm64)  KARCH=arm64  ; CROSS=aarch64-linux-gnu-     ; DEBARCH=arm64 ;;
armhf)  KARCH=arm    ; CROSS=arm-linux-gnueabihf-   ; DEBARCH=armhf ;;
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
# Created once and reused. Never built in-tree, which is what keeps `O=`
# working for every target.
if [ ! -d "$BUILD_TREE" ]; then
	echo "== creating build worktree =="
	git -C "$KERNEL_SRC" worktree add --detach "$BUILD_TREE" "$BUILD_REF"
fi

echo "== syncing driver sources =="
"$HERE/sync-driver.sh" "$BUILD_TREE"

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
release=$(make -s -C "$BUILD_TREE" O="$O" ARCH="$KARCH" \
	${CROSS:+CROSS_COMPILE="$CROSS"} kernelrelease)
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
		${CROSS:+CROSS_COMPILE="$CROSS"} KBUILD_DEBARCH="$DEBARCH" \
		-j"$JOBS" bindeb-pkg
else
	make -C "$BUILD_TREE" O="$O" ARCH="$KARCH" \
		${CROSS:+CROSS_COMPILE="$CROSS"} -j"$JOBS"
fi
elapsed=$(( $(date +%s) - start ))

echo
echo "built $TARGET ($release) in $((elapsed / 60))m $((elapsed % 60))s"

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
