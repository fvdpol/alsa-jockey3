#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Build the driver module for one target kernel, ready to deploy.
#
#   ./build_module.sh x86_64-debug
#   ./build_module.sh x86_64-debug --manifest    also record build-id -> git
#   ./build_module.sh x86_64-debug --uncommitted build sandbox sources as-is
#
# Builds from COMMITTED sources
# -----------------------------
# The driver is edited in this repository and must be committed to the kernel
# branch (feature/jockey3 in ~/sound) before a test build. Otherwise nothing
# identifies what was tested: the module would be compiled from whatever
# happened to be lying in a build worktree, and the manifest would name a
# revision that never contained those bytes.
#
# So this does not copy sources in. It checks that the branch's committed
# driver matches this repository, points the build worktree at that commit, and
# builds what is there. The kernel commit lands in the manifest alongside the
# repository revision, and both are real.
#
# --uncommitted restores the old copy-then-build behaviour for quick iteration.
# The manifest records kernel_driver_dirty: true, so such a build is still
# distinguishable after the fact -- but it should not be what a recorded test
# result was produced from.
#
# Why this is not build_jockey3.sh
# --------------------------------
# build_jockey3.sh builds in KERNEL_SRC (~/sound) IN TREE, and exists to run
# the L1 gates: checkpatch, W=12, kernel-doc, codespell, size. What it produces
# is a verdict, not a module anyone should load.
#
# A module that will actually be inserted has to match the running kernel's
# vermagic exactly, which means building against the object tree that produced
# that kernel -- ~/kbuild/<target>, with ~/sound-build as its source. Using
# ~/sound instead produces a module for whatever configuration that tree was
# last built with, which is a different kernel whenever a target kernel is in
# use. Debug targets make this unmissable: a KASAN kernel will not take a
# module built without KASAN.
#
# The two builds are therefore both correct and not interchangeable. Run the
# gates before committing; run this before testing on hardware.
#
# Environment:
#   KERNEL_SRC     source of truth, never built in     (default ~/sound)
#   BUILD_TREE     clean worktree to build from        (default ~/sound-build)
#   BUILD_OUTPUT   root of per-target object dirs      (default ~/kbuild)
#   BUILD_REF      kernel branch holding the driver    (default feature/jockey3)

set -eu

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)

KERNEL_SRC=${KERNEL_SRC:-$HOME/sound}
BUILD_TREE=${BUILD_TREE:-$HOME/sound-build}
BUILD_OUTPUT=${BUILD_OUTPUT:-$HOME/kbuild}
BUILD_REF=${BUILD_REF:-feature/jockey3}
DST=sound/usb/jockey3
MANIFEST=0
UNCOMMITTED=0
TARGET=""

while [ $# -gt 0 ]; do
	case "$1" in
	--manifest)    MANIFEST=1; shift ;;
	--uncommitted) UNCOMMITTED=1; shift ;;
	--help|-h)  sed -n '3,10p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
	-*) echo "unknown option: $1" >&2; exit 2 ;;
	*)  TARGET=$1; shift ;;
	esac
done
[ -n "$TARGET" ] || { echo "usage: build_module.sh <target> [--manifest]" >&2; exit 2; }

OBJ=$BUILD_OUTPUT/$TARGET
[ -d "$OBJ" ] || {
	echo "no object tree at $OBJ" >&2
	echo "build the target kernel first: ./build_kernel.sh $TARGET" >&2
	exit 2
}
[ -f "$OBJ/include/config/kernel.release" ] || {
	echo "$OBJ is not a configured kernel build" >&2; exit 2
}
RELEASE=$(cat "$OBJ/include/config/kernel.release")

# ARCH=/CROSS_COMPILE= for the target, from targets.yaml -- mirrors
# build_kernel.sh. Without this, `make` defaults to the host architecture, so
# a cross target built no differently from x86_64 fails deep in kbuild
# (missing arch-specific headers) rather than at a checkable point, and
# nothing here would have caught it before invoking make.
if ! spec_arch=$(python3 - "$REPO/tests/hw/targets.yaml" "$TARGET" <<-'PY'
	import sys, yaml
	targets = yaml.safe_load(open(sys.argv[1]))["targets"]
	spec = targets.get(sys.argv[2])
	if not spec or not spec.get("arch"):
	    sys.exit("unknown target '%s' or missing arch in targets.yaml" % sys.argv[2])
	print(spec["arch"])
	PY
); then
	echo "$spec_arch" >&2
	exit 2
fi

case "$spec_arch" in
x86_64) KARCH=x86   ; CROSS= ;;
i386)   KARCH=x86   ; CROSS= ;;
arm64)  KARCH=arm64 ; CROSS=aarch64-linux-gnu- ;;
armhf)  KARCH=arm   ; CROSS=arm-linux-gnueabihf- ;;
*) echo "unsupported architecture '$spec_arch'" >&2; exit 2 ;;
esac

if [ -n "$CROSS" ] && ! command -v "${CROSS}gcc" >/dev/null 2>&1; then
	echo "missing cross compiler ${CROSS}gcc" >&2
	exit 3
fi

if [ "$UNCOMMITTED" = 1 ]; then
	echo "*** --uncommitted: building unstaged sources; this module is not"
	echo "*** reproducible from any commit. Do not record a test result from it."
	# The file list lives in sync-driver.sh, shared with the other build
	# scripts. Two copies would drift, and the failure mode is a module that
	# silently omits a newly added source file.
	"$HERE/sync-driver.sh" "$BUILD_TREE" >/dev/null
else
	# Shared with build_kernel.sh: verify the branch holds what is here, then
	# point the worktree at that commit. The file list lives in
	# sync-driver.sh, so a new source file is covered the moment it is added
	# there rather than in three places.
	# Assigned first, deliberately. Inside `echo "$(...)"` the substitution's
	# exit status is discarded -- `set -e` sees only the successful echo -- so
	# a refusal from use-committed.sh was swallowed and the build carried on
	# with whatever the worktree happened to hold. That is precisely the drift
	# use-committed.sh exists to prevent, and it wrote a manifest claiming a
	# clean revision for a binary built from an --uncommitted experiment.
	if ! ref_desc=$("$HERE/use-committed.sh" "$KERNEL_SRC" "$BUILD_TREE" "$BUILD_REF"); then
		exit 3
	fi
	echo "building $ref_desc"
fi

# With plain M=, kbuild writes the module's own objects next to its sources
# in $BUILD_TREE -- one shared location regardless of target, since $BUILD_TREE
# is the same worktree for every target and only $OBJ (via O=) varies. Building
# arm64-prod right after x86_64-prod would silently overwrite the same .ko
# with the wrong architecture's binary, with nothing to stop it being handed to
# reload_driver.sh next. MO= (Documentation/kbuild/modules.rst) redirects the
# module's own build output to a separate directory while still reading
# configuration and headers from $OBJ; pointing it at $OBJ/$DST leaves the
# module at the same path a full build_kernel.sh build would also leave it at,
# so each target gets one stable, independent location, the same way
# $OBJ itself already does for the kernel object tree.
MOD_OUT=$OBJ/$DST

# A source tree that already holds build artifacts -- from a build predating
# MO=, or a stray plain M= invocation -- makes kbuild refuse an MO= build with
# "external module source tree is not clean". Harmless and fast when there is
# nothing to clean.
if compgen -G "$BUILD_TREE/$DST/*.o" >/dev/null 2>&1 || [ -f "$BUILD_TREE/$DST/Module.symvers" ]; then
	make -C "$BUILD_TREE" M=$DST clean >/dev/null
fi

make -C "$BUILD_TREE" -j"$(nproc)" O="$OBJ" ARCH="$KARCH" ${CROSS:+CROSS_COMPILE="$CROSS"} \
	M=$DST MO="$MOD_OUT" modules

KO=$MOD_OUT/snd-reloop-jockey3.ko
[ -f "$KO" ] || { echo "no module produced at $KO" >&2; exit 3; }

echo
echo "built for $TARGET ($RELEASE)"
/sbin/modinfo "$KO" | grep -E '^(vermagic|srcversion)' | sed 's/^/  /'
echo "  path: $KO"

# Refuse to hand over a module that cannot load. vermagic is what the kernel
# actually checks, so checking anything else here would be theatre.
VM=$(/sbin/modinfo "$KO" | sed -n 's/^vermagic: *//p')
case "$VM" in
"$RELEASE "*) ;;
*) echo "  *** vermagic does not start with '$RELEASE' -- will not load" >&2
   exit 3 ;;
esac

if [ "$MANIFEST" = 1 ]; then
	"$HERE/write-manifest.sh" "$KO" "$BUILD_TREE" "$RELEASE"
fi

echo
echo "deploy it with, on the test machine:"
echo "  tests/hw/actions/reload_driver.sh <path-to-ko>"
