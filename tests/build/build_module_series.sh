#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Build a loadable module from one commit of an exported patch series, for the
# per-patch hardware smoke test.
#
#   build_module_series.sh <target> --patch <n> [--manifest]
#   build_module_series.sh <target> <kernel-rev> [--manifest]
#
#   --patch <n>    the n-th commit (1-based) of $SERIES, e.g. --patch 4
#   <kernel-rev>   any commit in the kernel repository instead
#   --manifest     record build-id -> commit, for the test ledger
#
# Environment:
#   KERNEL_SRC     kernel repository                 (default ~/sound)
#   BUILD_TREE     worktree the source is read from  (default ~/sound-build)
#   BUILD_OUTPUT   root of per-target object dirs    (default ~/kbuild)
#   SERIES         range --patch counts in   (default origin/for-next..jockey3-v5)
#   SERIES_OUT     where modules are written (default ~/kbuild-series)
#
# This is build_module.sh for the v5 split, and deliberately a separate script:
# build_module.sh insists that feature/jockey3 matches this repository, which
# is the right rule for normal work and meaningless for an intermediate patch.
# What it does differently:
#
# - The commit is the identity. It builds exactly the named kernel commit, as
#   exported by export-series.sh, with no comparison against this repository.
#
# - The module goes to $SERIES_OUT/modules/<target>/<nn>-<sha>/, never to
#   ~/kbuild/<target>/sound/usb/jockey3 where build_module.sh leaves its own,
#   so one can never be mistaken for the other and each patch keeps its .ko.
#
# - The kernel base is checked. The .ko is compiled against headers from the
#   source tree, so it is only ABI-compatible with the target kernel if the
#   series commit and the commit that kernel was built from differ in nothing
#   but the driver and its glue. vermagic cannot tell: it would match a module
#   built on a different origin/for-next just the same. The target kernel's
#   source commit is read from the "-g<sha>" in its object tree's
#   debian/changelog, which build_kernel.sh's bindeb-pkg writes.
#
# - The manifest names the series. write-manifest.sh records this repository's
#   HEAD as the driver revision, which for a series build would be wrong; the
#   git_* fields are rewritten to the series-branch commit that export-series.sh
#   produced the kernel commit from (same author date and subject), and
#   series_kernel_commit / series_patch are added.
#
# It reuses BUILD_TREE, the disposable worktree build_module.sh also uses;
# build_module.sh and build_kernel.sh move it back to their own commit on
# their next run. Do not run this concurrently with either of them.

set -eu

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)

KERNEL_SRC=${KERNEL_SRC:-$HOME/sound}
BUILD_TREE=${BUILD_TREE:-$HOME/sound-build}
BUILD_OUTPUT=${BUILD_OUTPUT:-$HOME/kbuild}
SERIES=${SERIES:-origin/for-next..jockey3-v5}
SERIES_OUT=${SERIES_OUT:-$HOME/kbuild-series}
DST=sound/usb/jockey3
MANIFEST=0
TARGET=
REV=
PATCH=

# The paths a series commit may differ in from the kernel it is loaded into.
# Same list as export-series.sh's OWNED_PATHS.
DRIVER_PATHS=(sound/usb/jockey3 Documentation/sound/cards MAINTAINERS
	      sound/usb/Kconfig sound/usb/Makefile)

usage() {
	sed -n '/^#   build_module_series.sh/,/^# Environment/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2
	exit 2
}

while [ $# -gt 0 ]; do
	case $1 in
	--patch)	PATCH=${2:?}; shift 2 ;;
	--manifest)	MANIFEST=1; shift ;;
	-h|--help)	usage ;;
	-*)		echo "unknown option: $1" >&2; usage ;;
	*)		if [ -z "$TARGET" ]; then TARGET=$1
			elif [ -z "$REV" ]; then REV=$1
			else usage; fi
			shift ;;
	esac
done
[ -n "$TARGET" ] || usage
if [ -n "$REV" ] && [ -n "$PATCH" ]; then usage; fi
if [ -z "$REV" ] && [ -z "$PATCH" ]; then usage; fi

K() { git -C "$KERNEL_SRC" "$@"; }

if [ -n "$PATCH" ]; then
	REV=$(K rev-list --reverse "$SERIES" | sed -n "${PATCH}p")
	[ -n "$REV" ] || { echo "no patch $PATCH in $SERIES" >&2; exit 2; }
fi
SHA=$(K rev-parse --verify -q "$REV^{commit}") || {
	echo "no commit '$REV' in $KERNEL_SRC" >&2; exit 2; }
SHORT=$(K rev-parse --short "$SHA")
SUBJECT=$(K log -1 --format=%s "$SHA")
[ -n "$PATCH" ] || PATCH=$(K rev-list --reverse "$SERIES" 2>/dev/null | grep -n -x "$SHA" | cut -d: -f1)

OBJ=$BUILD_OUTPUT/$TARGET
[ -f "$OBJ/include/config/kernel.release" ] || {
	echo "no configured kernel build at $OBJ; run ./build_kernel.sh $TARGET" >&2; exit 2; }
RELEASE=$(cat "$OBJ/include/config/kernel.release")

# --------------------------------------------------------- kernel base check
kernel_src_sha=$(sed -n '1s/.*-g\([0-9a-f]\{7,\}\)[-)].*/\1/p' "$OBJ/debian/changelog" 2>/dev/null)
[ -n "$kernel_src_sha" ] || {
	echo "cannot tell which commit the $TARGET kernel was built from" >&2
	echo "(no -g<sha> in $OBJ/debian/changelog)" >&2
	exit 3
}
K rev-parse --verify -q "$kernel_src_sha^{commit}" >/dev/null || {
	echo "the $TARGET kernel was built from $kernel_src_sha, unknown in $KERNEL_SRC" >&2
	exit 3
}
outside=$(K diff --name-only "$kernel_src_sha" "$SHA" -- . \
	$(printf ":(exclude)%s " "${DRIVER_PATHS[@]}"))
if [ -n "$outside" ]; then
	echo "$SHORT and the $TARGET kernel ($kernel_src_sha) differ outside the driver:" >&2
	echo "$outside" | head -10 | sed 's/^/    /' >&2
	echo "a module built from it would not be ABI-compatible with that kernel;" >&2
	echo "rebuild the kernel from the series base (build_kernel.sh $TARGET)" >&2
	exit 3
fi

# ------------------------------------------------------------- architecture
# Mirrors build_module.sh: ARCH/CROSS_COMPILE from targets.yaml.
spec_arch=$(python3 - "$REPO/tests/hw/targets.yaml" "$TARGET" <<-'PY'
	import sys, yaml
	spec = yaml.safe_load(open(sys.argv[1]))["targets"].get(sys.argv[2])
	if not spec or not spec.get("arch"):
	    sys.exit("unknown target '%s' or missing arch in targets.yaml" % sys.argv[2])
	print(spec["arch"])
	PY
) || exit 2
case "$spec_arch" in
x86_64|i386)	KARCH=x86   ; CROSS= ;;
arm64)		KARCH=arm64 ; CROSS=aarch64-linux-gnu- ;;
armhf)		KARCH=arm   ; CROSS=arm-linux-gnueabihf- ;;
*) echo "unsupported architecture '$spec_arch'" >&2; exit 2 ;;
esac

# -------------------------------------------------------------------- build
echo "building patch ${PATCH:-?} $SHORT \"$SUBJECT\" for $TARGET ($RELEASE)"
if [ ! -d "$BUILD_TREE" ]; then
	K worktree add --detach "$BUILD_TREE" "$SHA" >/dev/null
else
	# Disposable scratch, as in use-committed.sh.
	git -C "$BUILD_TREE" checkout --detach --force --quiet "$SHA"
fi

MOD_OUT=$SERIES_OUT/modules/$TARGET/$(printf '%02d' "${PATCH:-0}")-$SHORT
rm -rf "$MOD_OUT"
mkdir -p "$MOD_OUT"

# MO= refuses a module source directory holding build artifacts; see
# build_module.sh.
if compgen -G "$BUILD_TREE/$DST/*.o" >/dev/null 2>&1 || [ -f "$BUILD_TREE/$DST/Module.symvers" ]; then
	make -C "$BUILD_TREE" M=$DST clean >/dev/null
fi

make -C "$BUILD_TREE" -j"$(nproc)" O="$OBJ" ARCH="$KARCH" ${CROSS:+CROSS_COMPILE="$CROSS"} \
	M=$DST MO="$MOD_OUT" modules

KO=$MOD_OUT/snd-reloop-jockey3.ko
[ -f "$KO" ] || { echo "no module produced at $KO" >&2; exit 3; }

VM=$(/sbin/modinfo "$KO" | sed -n 's/^vermagic: *//p')
case "$VM" in
"$RELEASE "*) ;;
*) echo "  *** vermagic '$VM' does not start with '$RELEASE' -- will not load" >&2
   exit 3 ;;
esac

echo
echo "built patch ${PATCH:-?} for $TARGET ($RELEASE), kernel base $kernel_src_sha"
echo "  vermagic: $VM"
echo "  path: $KO"

# ----------------------------------------------------------------- manifest
if [ "$MANIFEST" = 1 ]; then
	out=$("$HERE/write-manifest.sh" "$KO" "$BUILD_TREE" "$RELEASE")
	echo "$out"
	json=${out#manifest: }

	# The series-branch commit this kernel commit was exported from:
	# export-series.sh keeps the author date and the message.
	adate=$(K log -1 --format=%aI "$SHA")
	src=$(git -C "$REPO" log --all --format='%H%x09%aI%x09%s' |
		awk -F'\t' -v d="$adate" -v s="$SUBJECT" '$2 == d && $3 == s {print $1}')
	[ "$(echo "$src" | grep -c .)" = 1 ] || {
		echo "  (no unique series-branch commit for $SHORT; git_* fields left as HEAD)" >&2
		src=
	}
	python3 - "$json" "$SHA" "${PATCH:-}" "$src" "$REPO" <<-'PY'
	import json, subprocess, sys
	path, ksha, patch, src, repo = sys.argv[1:6]
	m = json.load(open(path))
	m["build_kind"] = "series"
	m["series_kernel_commit"] = ksha
	m["series_patch"] = int(patch) if patch else None
	if src:
	    git = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True,
	                                   text=True).stdout.strip()
	    m["git_hash"] = src
	    m["git_describe"] = git("describe", "--always", src)
	    names = git("branch", "--contains", src, "--format=%(refname:short)").split()
	    m["git_branch"] = names[0] if len(names) == 1 else ",".join(names)
	    m["dirty"] = False
	json.dump(m, open(path, "w"), indent=2)
	open(path, "a").write("\n")
	PY
fi

echo
echo "deploy it with, on the test machine:"
echo "  tests/hw/actions/reload_driver.sh <path-to-ko>   # stage it as snd-reloop-jockey3.ko"
