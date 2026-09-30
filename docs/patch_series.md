# Building the patch series

v1–v4 were submitted as one patch. The maintainer asked for v5 as a series
that can be reviewed one piece at a time. This document describes how that
series is produced from this repository. What the individual patches are is
tracked in the workspace plan, not here.

## The rules

- **Every commit builds, and is safe on hardware.** Someone bisecting can boot
  any point in the series with a Jockey 3 plugged in. So no step may drive the
  device in a way the full driver never does. In practice, the transport patch
  starts all three bulk rings from the beginning, and later patches change only
  what goes into or out of the packets.
- **No dead code.** A helper arrives in the same patch as its first user.
- **Split only.** The tip of the series is byte-identical to the driver on
  `main`. No fixes and no restructuring ride along, so the review of the
  squashed version still applies.

## Where the series lives

The split is ordinary commits on a branch of this repository, one commit per
patch. That way the existing build and test tooling works at every step.

```
main ─── … ─── X                          the driver as it is
                \
                 S ── P1 ── P2 ── … ── Pn   series branch
```

- `S` is a setup commit that strips the driver files down to the first
  patch's state. It is not part of the series and never leaves this
  repository.
- `P1..Pn` each add one piece back, and carry the final kernel commit message:
  subject, body, `Assisted-by:`, `Signed-off-by:`.
- `git diff X Pn -- <driver files>` must be empty.
  `export-series.sh --expect-repo main` checks exactly this: it builds
  `main`'s kernel tree the same way and compares it with the tip.

A fix to any patch is a rebase on this branch followed by a fresh export.
Nothing is ever edited on the kernel side.

## Exporting to the kernel tree

```sh
tests/build/export-series.sh --branch jockey3-v5 --expect-repo main S..Pn
```

For each commit in the range, this writes one commit on `jockey3-v5` in
`~/sound`, on top of `origin/for-next`. It keeps the author, date and message
unchanged. How it works:

- **Each commit is built fresh** from the base, plus the driver files as they
  are in that commit, plus the glue. A file the commit does not have is
  absent, not left over from an earlier step.
- **The file list is `sync-driver.sh --list`**, the same as every other build.
  A mapped file missing from a commit is skipped.
- **The glue is in `tests/build/kernel-glue/`.** These are the lines outside
  `sound/usb/jockey3/` that exist only in the kernel tree: the
  `sound/usb/Kconfig` and `Makefile` hooks, `MAINTAINERS`, and the
  `cards/index.rst` entry. Each patch goes into the first commit that contains
  its trigger file (`Kconfig`, or `jockey3.rst`), so reordering the series
  needs no change to the script. The `MAINTAINERS` line
  `F: Documentation/sound/cards/jockey3.rst` is in the docs glue, not the
  Kbuild glue, so no commit has an entry pointing at a file it does not
  contain.
- **Commits that change no driver file are skipped**, for example a `tests:`
  commit.
- **No working tree is touched.** Commits are assembled with git plumbing
  against a private index. The script refuses to move a branch that some
  worktree has checked out, and refuses `feature/jockey3` outright.
- **A commit without `Signed-off-by:` is refused.** This also catches a range
  that starts one commit early and so takes in `S`. With `--dry-run` it is
  only a warning, so `main~1..main --dry-run --expect-repo main` works as a
  self-test of the script.
- **`--expect-repo <rev>`** compares the tip against the driver at `<rev>` in
  this repository. **`--expect <ref>`** compares it against a kernel ref, e.g.
  `feature/jockey3`. Both look only at the driver and glue paths, and fail on
  any difference. `--dry-run` builds the commits without moving the branch.

The export takes seconds and starts over every time, so rerunning it after
every change on the series branch is the intended use.

Use `--expect-repo main` as the check. `--expect feature/jockey3` compares
against v4, which differs by one post-v4 fix, `ploytec_codec_kunit.c` (the
batch-equals-single buffers moved to the heap). That comparison is only useful
for seeing what v5 changes relative to v4.

## Gating each commit

`build_jockey3.sh` cannot gate a series. It syncs from this repository's
working tree, so it would overwrite every intermediate state with the final
code and report green. Instead, run the gates on the exported branch, one
commit at a time:

```sh
git -C ~/sound checkout jockey3-v5
git -C ~/sound rebase -x '<gate command>' origin/for-next
```

Each step must, at minimum:

- remove `sound/usb/jockey3/*.o`, since an incremental build has reported zero
  warnings having compiled nothing
- build with `W=12`
- run `kernel-doc -Wall` over the driver sources that exist at that commit
- run `checkpatch.pl --strict -g HEAD`, which unlike the diff-based gate sees
  the subject, sign-off and trailers
- run KUnit, from the patch that adds it onward

A wrapper script for this does not exist yet; that is the next piece of
tooling. Checking out `jockey3-v5` in `~/sound` leaves `feature/jockey3`'s
state behind, so switch back before using `build_jockey3.sh` or
`build_module.sh` again.
