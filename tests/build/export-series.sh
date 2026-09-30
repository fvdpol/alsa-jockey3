#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Turn a range of commits in this repository into a patch series on a kernel
# branch: one kernel commit per repository commit, same message, same author.
#
#   export-series.sh [options] <rev-range>
#
#   <rev-range>        commits to replay, as for git rev-list, e.g.
#                      series/v5-strip..series/v5 (the start is excluded)
#   --branch <name>    kernel branch to write          (default jockey3-series)
#   --base <ref>       upstream the series applies to  (default origin/for-next)
#   --expect-repo <rev>
#                      compare the tip against the driver as it is at <rev>
#                      in this repository, e.g. main, and fail on any
#                      difference -- the "split only" check
#   --expect <ref>     the same against a kernel ref, e.g. feature/jockey3
#   --dry-run          build the commits but do not move the branch
#
# Environment:
#   KERNEL_SRC         kernel repository               (default ~/sound)
#
# Every kernel commit is built from scratch as <base> + the driver files as
# they are in that repository commit + the kernel-side glue. Nothing is carried
# over from the previous step, so a file that does not exist yet at some point
# in the series is absent from that commit, not left behind by an earlier one.
#
# The file mapping is sync-driver.sh's, so this covers exactly what every other
# build covers. A mapped file missing from a commit is skipped: early patches
# in a series do not have the MIDI code, the KUnit tests or the documentation.
#
# The glue lives in kernel-glue/: the lines outside sound/usb/jockey3 that
# the driver needs (Kbuild hooks, MAINTAINERS, the docs index). Each patch is
# applied in the first commit that contains the file it refers to, rather than
# at a fixed position, so the series can be reordered without touching this
# script.
#
# Commits are assembled with git plumbing against a private index, so no
# working tree is checked out, modified or rebuilt. The branch is refused if a
# worktree has it checked out, since moving it underneath one would leave that
# worktree's files out of step with its HEAD. feature/jockey3 -- the submitted
# v4 -- is refused outright.
#
# The repository commits must carry the final message, trailers included
# (Signed-off-by, Assisted-by). This script copies them and adds nothing; a
# commit without a Signed-off-by is refused, which also catches a range that
# accidentally starts one commit early and takes in the setup commit. With
# --dry-run it is only a warning, so any stretch of main can be replayed as a
# check.

set -eu
shopt -s inherit_errexit	# kernel_tree runs inside $(...)

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
GLUE=$HERE/kernel-glue

KERNEL_SRC=${KERNEL_SRC:-$HOME/sound}
BRANCH=jockey3-series
BASE=origin/for-next
EXPECT=
EXPECT_REPO=
DRY_RUN=0
RANGE=

usage() {
	sed -n '/^#   export-series.sh/,/^# Environment/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2
	exit 2
}

while [ $# -gt 0 ]; do
	case $1 in
	--branch)	BRANCH=${2:?}; shift 2 ;;
	--base)		BASE=${2:?}; shift 2 ;;
	--expect)	EXPECT=${2:?}; shift 2 ;;
	--expect-repo)	EXPECT_REPO=${2:?}; shift 2 ;;
	--dry-run)	DRY_RUN=1; shift ;;
	-h|--help)	usage ;;
	-*)		echo "unknown option: $1" >&2; usage ;;
	*)		[ -z "$RANGE" ] || usage; RANGE=$1; shift ;;
	esac
done
[ -n "$RANGE" ] || usage

# glue patch : repository file whose first appearance brings it in. Applied in
# this order, so a later patch may depend on an earlier one's context.
GLUE_TRIGGERS=(
	"kbuild.patch:Kconfig"
	"docs-index.patch:Documentation/sound/cards/jockey3.rst"
)

# The paths this series may touch. Anything else in a commit is a bug here.
OWNED_PATHS=(sound/usb/jockey3 Documentation/sound/cards MAINTAINERS
	     sound/usb/Kconfig sound/usb/Makefile)

K() { git -C "$KERNEL_SRC" "$@"; }
R() { git -C "$REPO" "$@"; }

[ "$BRANCH" != feature/jockey3 ] || {
	echo "refusing to overwrite feature/jockey3, the submitted v4" >&2; exit 2; }

base_sha=$(K rev-parse --verify -q "$BASE^{commit}") || {
	echo "no ref '$BASE' in $KERNEL_SRC" >&2; exit 2; }
if [ -n "$EXPECT" ]; then
	expect_sha=$(K rev-parse --verify -q "$EXPECT^{commit}") || {
		echo "no ref '$EXPECT' in $KERNEL_SRC" >&2; exit 2; }
fi
if [ -n "$EXPECT_REPO" ]; then
	R rev-parse --verify -q "$EXPECT_REPO^{commit}" >/dev/null || {
		echo "no revision '$EXPECT_REPO' in $REPO" >&2; exit 2; }
fi

if [ "$DRY_RUN" = 0 ] &&
   K worktree list --porcelain | grep -qx "branch refs/heads/$BRANCH"; then
	echo "$BRANCH is checked out in a worktree of $KERNEL_SRC;" >&2
	echo "switch that worktree away from it first (or use --dry-run)" >&2
	exit 2
fi

mapfile -t commits < <(R rev-list --reverse --topo-order "$RANGE")
[ ${#commits[@]} -gt 0 ] || { echo "no commits in $RANGE" >&2; exit 2; }
if [ -n "$(R rev-list --min-parents=2 "$RANGE")" ]; then
	echo "$RANGE contains merge commits; a patch series must be linear" >&2
	exit 2
fi

mapfile -t mapping < <("$HERE/sync-driver.sh" --list)

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
export GIT_INDEX_FILE=$tmp/index

# The kernel tree for one repository commit: base + its driver files + glue.
kernel_tree() {
	local sha=$1 pair src dst entry blob patch trigger

	K read-tree "$base_sha"

	for pair in "${mapping[@]}"; do
		src=${pair%%:*}
		dst=${pair#*:}
		entry=$(R ls-tree "$sha" -- "$src")
		[ -n "$entry" ] || continue
		blob=$(R cat-file blob "$sha:$src" | K hash-object -w --stdin)
		K update-index --add --cacheinfo "${entry%% *},$blob,$dst"
	done

	for pair in "${GLUE_TRIGGERS[@]}"; do
		patch=${pair%%:*}
		trigger=${pair#*:}
		[ -n "$(R ls-tree "$sha" -- "$trigger")" ] || continue
		K apply --cached "$GLUE/$patch" || {
			echo "kernel-glue/$patch does not apply to $BASE" >&2; exit 1; }
	done

	K write-tree
}

parent=$base_sha
prev_tree=$(K rev-parse "$base_sha^{tree}")
n=0
for sha in "${commits[@]}"; do
	tree=$(kernel_tree "$sha")
	subject=$(R log -1 --format=%s "$sha")
	short=$(R rev-parse --short "$sha")

	if [ "$tree" = "$prev_tree" ]; then
		echo "skip  $short $subject  (no driver change)"
		continue
	fi

	stray=$(K diff-tree -r --name-only "$prev_tree" "$tree" -- . \
		$(printf ":(exclude)%s " "${OWNED_PATHS[@]}"))
	if [ -n "$stray" ]; then
		echo "$short would touch paths outside the driver:" >&2
		echo "$stray" | sed 's/^/    /' >&2
		exit 1
	fi

	R log -1 --format=%B "$sha" > "$tmp/msg"
	if ! grep -q '^Signed-off-by: ' "$tmp/msg"; then
		if [ "$DRY_RUN" = 0 ]; then
			echo "$short \"$subject\" has no Signed-off-by" >&2
			echo "(is the range starting one commit early?)" >&2
			exit 1
		fi
		echo "warn  $short has no Signed-off-by" >&2
	fi

	parent=$(GIT_AUTHOR_NAME=$(R log -1 --format=%an "$sha") \
		 GIT_AUTHOR_EMAIL=$(R log -1 --format=%ae "$sha") \
		 GIT_AUTHOR_DATE=$(R log -1 --format=%aI "$sha") \
		 K commit-tree "$tree" -p "$parent" -F "$tmp/msg")
	prev_tree=$tree
	n=$((n + 1))
	printf '%3d   %s %s  (from %s)\n' "$n" "$(K rev-parse --short "$parent")" \
		"$subject" "$short"
done

[ "$n" -gt 0 ] || { echo "no commit in $RANGE changes the driver" >&2; exit 1; }

if [ "$DRY_RUN" = 1 ]; then
	echo "dry run: $n commits built, tip $(K rev-parse --short "$parent"); $BRANCH not moved"
else
	K update-ref -m "export-series.sh $RANGE" "refs/heads/$BRANCH" "$parent"
	echo "$BRANCH: $n commits on $BASE, tip $(K rev-parse --short "$parent")"
fi

# compare <label> <tree-ish>: the tip against it, over the paths this owns
status=0
compare() {
	if K diff --quiet "$2" "$parent" -- "${OWNED_PATHS[@]}"; then
		echo "tip matches $1"
	else
		echo "tip differs from $1:"
		K diff --stat "$2" "$parent" -- "${OWNED_PATHS[@]}"
		status=1
	fi
}
[ -z "$EXPECT_REPO" ] || compare "$EXPECT_REPO (this repository)" "$(kernel_tree "$EXPECT_REPO")"
[ -z "$EXPECT" ] || compare "$EXPECT" "$expect_sha"
exit $status
