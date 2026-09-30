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
code and report green. `tests/build/gate-series.sh` gates the exported
commits instead, one at a time:

```sh
tests/build/gate-series.sh                      # origin/for-next..jockey3-v5
tests/build/gate-series.sh --only 4             # just the 4th patch
tests/build/gate-series.sh --variants x86_64 --no-kunit   # quick pass
```

It checks each commit out in its own detached worktree, `~/sound-series`,
which is created on first use. It builds into its own object trees under
`~/kbuild-series`. So it never touches `~/sound`'s checkout, `~/sound-build`
or `~/kbuild`, and `export-series.sh` can move `jockey3-v5` between runs.

Per commit, over whatever driver files that commit has:

| Gate | What |
|---|---|
| build | `W=12 C=1 M=sound/usb/jockey3`, per variant, with the module's objects deleted first. The default variants cover every target architecture: `x86_64`, `x86_64-ref` (reference codec), `i386`, `arm64` and `armhf`. The optimized codec differs by word size, so both 32- and 64-bit builds are needed. A missing cross compiler fails the gate. A `-ref` variant is n/a until the commit's Kconfig has the option. |
| kdoc | `kernel-doc -Wall -Werror`, `.c` and `.h` |
| checkpatch | `--strict -g <commit>`, so the subject, sign-off and trailers are checked too |
| spell | codespell over the sources and the `.rst` |
| rst | `rst2html --strict`, once `jockey3.rst` exists |
| unused | every non-static `ploytec_*()` must have a caller outside the KUnit tests. The compiler only catches dead static functions. |
| kunit | UML, once the suite exists; again with the reference codec once that option exists |

The build filters the same known-benign `-Wshadow` hits as
`build_jockey3.sh`, plus sparse's own report of the `__ret` shadow. Before
anything runs, the script checks that `scripts/checker-valid.sh` accepts
sparse; otherwise `C=1` passes having checked nothing.

Each config is copied from `tests/configs/<arch>-debug.config` before every
build, because `olddefconfig` silently drops a symbol that the commit's
Kconfig does not define yet, and would not bring it back later.

Logs go to `~/kbuild-series/logs/<nn>-<sha>/`. The exit status is the number
of failing commits.

The first run is slow: `modules_prepare` runs once per variant and KUnit
builds UML from scratch. Later runs rebuild only the module.

## Hardware testing

Every patch that touches code gets an x86_64 run with the `smoke` profile;
only purely documentation patches are exempt. No other targets and no
deeper profiles: the tip is the driver that has already been validated on
every target, so each intermediate state only needs a sanity check on the
primary target. Compile coverage of the other architectures comes from the
gate above.

The module for such a run comes from `tests/build/build_module_series.sh`,
not `build_module.sh`. The latter only builds `feature/jockey3`, and only
when it matches this repository.

```sh
tests/build/build_module_series.sh x86_64-prod --patch 4 --manifest
# -> ~/kbuild-series/modules/x86_64-prod/04-<sha>/snd-reloop-jockey3.ko
```

- **It builds the n-th commit of `origin/for-next..jockey3-v5`**, or any
  kernel commit named instead of `--patch`, against the target's object tree
  in `~/kbuild`.
- **Each patch gets its own output directory**, so `build_module.sh`'s module
  is never overwritten.
- **It refuses a commit that differs from the target kernel's own source
  outside the driver and glue paths.** Such a module would pass the vermagic
  check and still be built against the wrong headers. It reads the kernel's
  source commit from the `-g<sha>` in the object tree's `debian/changelog`.
  If `origin/for-next` moves, rebase the series, not the kernel, or rebuild
  the kernel.
- **The manifest records `build_kind: series`, `series_patch` and
  `series_kernel_commit`.** Its `git_hash` points at the series-branch commit
  the patch was exported from, not this repository's `HEAD`.
- **It borrows `~/sound-build`**, which `build_module.sh` and
  `build_kernel.sh` move back on their next run. Don't run it concurrently
  with either.
