#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Point a build worktree at committed driver sources, and refuse if this
# repository has moved on.
#
#   use-committed.sh <kernel-src> <build-tree> [ref]
#
# Shared by build_module.sh and build_kernel.sh so that anything which produces
# a binary somebody will boot or insert is reproducible from a commit.
#
# The problem it exists to prevent is quiet. A build worktree is created once
# and then reused; nothing moves it forward, so it sits at whatever commit it
# was last left on -- possibly by a different script. Copying the working tree
# over the top hides that, because the driver sources end up right even when
# the tree underneath them is months old, and the manifest records only whether
# THIS repository was clean. A kernel was built that way on 2026-08-09 and its
# manifest read "2f9ac17-dirty" against a worktree nobody had updated.
#
# So: compare the driver in the branch against the driver here, and if they
# agree, move the worktree to that commit and build what is committed.

set -eu

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)

KERNEL_SRC=${1:?usage: use-committed.sh <kernel-src> <build-tree> [ref]}
BUILD_TREE=${2:?usage: use-committed.sh <kernel-src> <build-tree> [ref]}
REF=${3:-feature/jockey3}

SHA=$(git -C "$KERNEL_SRC" rev-parse --verify "$REF" 2>/dev/null) || {
	echo "no ref '$REF' in $KERNEL_SRC" >&2; exit 2; }

# Compare the files rather than trusting a workflow. The property that matters
# is that rebuilding from this commit produces the same driver, and only the
# bytes can say that. The mapping comes from sync-driver.sh, so a newly added
# source file -- or documentation file, at whatever path -- is covered here
# the moment it is covered there. dst is already relative to the kernel tree
# root (sync-driver.sh does not assume everything lands under sound/usb/jockey3).
drift=""
while IFS= read -r pair; do
	src=${pair%%:*}
	dst=${pair#*:}
	a=$(git -C "$KERNEL_SRC" show "$SHA:$dst" 2>/dev/null | sha256sum | cut -d' ' -f1)
	b=$(sha256sum "$REPO/$src" 2>/dev/null | cut -d' ' -f1)
	[ "$a" = "$b" ] || drift="$drift $src"
done < <("$HERE/sync-driver.sh" --list)

if [ -n "$drift" ]; then
	echo "the driver in $REF does not match this repository:" >&2
	for f in $drift; do echo "    $f" >&2; done
	echo >&2
	echo "commit it into the kernel branch first, so the build is reproducible" >&2
	echo "and the manifest names a revision that contains it:" >&2
	echo >&2
	echo "    tests/build/sync-driver.sh $KERNEL_SRC" >&2
	echo "    git -C $KERNEL_SRC add sound/usb/jockey3 Documentation/sound/cards/jockey3.rst" >&2
	echo "    git -C $KERNEL_SRC commit --amend --no-edit   # or a new commit" >&2
	echo >&2
	echo "or pass --uncommitted to build them as they are." >&2
	exit 3
fi

# Create the worktree if it is missing, and move it to the commit either way.
# Creating-only-if-absent is what let it drift: it was created once at this ref
# and then never advanced again.
if [ ! -d "$BUILD_TREE" ]; then
	git -C "$KERNEL_SRC" worktree add --detach "$BUILD_TREE" "$SHA" >/dev/null
else
	# --force because the worktree is disposable scratch whose only job is to
	# hold committed source; leftovers from an --uncommitted build are
	# discarded. Untracked build output is left alone so rebuilds stay
	# incremental.
	git -C "$BUILD_TREE" checkout --detach --force --quiet "$SHA"
fi

echo "$REF at $(git -C "$KERNEL_SRC" rev-parse --short "$SHA")"
