# Where the code lives

This driver exists in several places at once, each for a different reason, and
confusing them wastes an afternoon at best and attributes a test result to the
wrong revision at worst. This is the map.

The short version: **one place is edited, everywhere else is a copy.**

```
                 ~/jockey3_linux/alsa-jockey3/          <-- the only place edited
                 (git: github.com/fvdpol/alsa-jockey3)
                              |
                              |  tests/build/sync-driver.sh   (cp -u, never -p)
              +---------------+----------------+
              |                                |
              v                                v
        ~/sound/                        ~/sound-build/
        sound/usb/jockey3/              sound/usb/jockey3/
        in-tree build                   clean worktree, never built in-tree
        git: tiwai/sound.git                   |
        branch feature/jockey3                 |  make O=
        base   origin/for-next                 v
              |                          ~/kbuild/<target>/
              |  make M=                 object tree + .deb packages
              v                                |
        L1 gates only                          |  build_module.sh -> .ko
        (verdict, not a                        v
         loadable module)                 alsa-test: /lib/modules/<release>/
```

---

## The five locations

| # | Path | What it is | Size |
|---|---|---|---|
| 1 | `~/jockey3_linux/alsa-jockey3/` | **Dev sandbox.** The only place source is edited. Git remote is the project's own GitHub repo. | small |
| 2 | `~/sound/` | **Staging / upstream validation.** A clone of Takashi's `sound.git`, branch `feature/jockey3` on top of `origin/for-next`. Built **in tree**. | 24 GB |
| 3 | `~/sound-build/` | **Clean worktree of #2**, detached at the same commit. Source for every `O=` build. Never built in tree. | 871 MB |
| 4 | `~/kbuild/<target>/` | **Target object trees** plus the `.deb` packages they produce. One directory per target. | 19 GB |
| 5 | `~/sound-kunit/` | **Second worktree of #2**, for `run_kunit.sh`. | 1.7 GB |

### 1. The dev sandbox — `~/jockey3_linux/alsa-jockey3/`

Where all editing happens, and the only location under the project's own git
history. Everything else receives copies.

It also lives on a Seafile share, which is how the test machine gets the test
framework. **`.git` is excluded from that share**, via `seafile-ignore.txt` at
the library root. Syncing a git object store through a file-sync tool corrupts
it: Seafile copies the objects as ordinary files with no ordering guarantee,
while git requires objects to exist before refs point at them, and `git gc`
deletes packfiles another machine still references. The observed result was a
checkout where every ref pointed at a missing object while the working-tree
files were byte-identical. Move revisions between machines with git itself.

`make` here is an out-of-tree build against `/usr/src/linux-source-6.12`:

```sh
cd ~/jockey3_linux/alsa-jockey3 && make
```

**This is a compile check and nothing more.** The resulting `.ko` matches
neither the dev container's own kernel nor any test target, so it cannot be
loaded anywhere. It answers "does it still compile", quickly. Nothing else.

### 2. Staging — `~/sound/`

The upstream tree, so that what is submitted is a patch against what
maintainers actually have:

```
origin    git://git.kernel.org/pub/scm/linux/kernel/git/tiwai/sound.git
branch    feature/jockey3
base      origin/for-next
```

Rebasing, patch preparation and the L1 gates happen here. It is built **in
tree** (`make M=sound/usb/jockey3`), which is why it needs a separate worktree
for anything using `O=`.

What it produces is a **verdict, not a deployable module**. Its `.config` is
whatever was last put there, and its objects may predate that — after the
target configs landed, `.config` said `-alsa-debug` while `make kernelrelease`
still said `7.2.0-rc5+`. A module built here is not what you want to insert.

### 3. The build worktree — `~/sound-build/`

A `git worktree` of #2, detached at the same commit. It exists for one reason:
kbuild refuses an out-of-tree (`O=`) build from a source tree that already
holds in-tree output, and #2 is built in tree. Reaching for `make mrproper` on
#2 instead would discard 24 GB and roughly forty minutes every time a target
kernel is built.

Plain `M=` writes a module's own objects next to its sources, but
`build_module.sh` passes `MO=` to redirect them into the target's own
`~/kbuild/<target>/sound/usb/jockey3/` instead (see below), so this worktree's
`sound/usb/jockey3/` holds source only, never build output.

### 4. Target builds — `~/kbuild/<target>/`

One object tree per target (`x86_64-debug`, and later `arm64-prod` and
friends), plus the `.deb` packages built from it. `include/config/kernel.release`
in each is the authoritative answer to "which kernel is this".

This is what a deployable module must be built against, because **`vermagic`
must match the running kernel exactly** — and on a debug target the KASAN
mismatch makes that unmissable rather than subtle.

`build_module.sh` also leaves the module itself here, at
`sound/usb/jockey3/snd-reloop-jockey3.ko` under this target's own directory —
the same path a full `build_kernel.sh` build already leaves it at. It does
this with `MO=` (`Documentation/kbuild/modules.rst`), which redirects an
external module's own build output to a separate directory while `O=` still
supplies the configuration and headers. Without it, `M=` alone writes the
module's objects next to its sources in `~/sound-build`, one shared location
regardless of target — so building `arm64-prod` right after `x86_64-prod`
silently overwrote the same `.ko` with the wrong architecture's binary, with
nothing to stop `reload_driver.sh` loading it next. With `MO=`, each target's
module — like its kernel object tree — has its own stable location, and two
targets can be built one after the other (or tested concurrently) without
either clobbering the other.

### 5. The KUnit worktree — `~/sound-kunit/`

A second worktree of #2, used by `tests/codec/run_kunit.sh` for UML and the
QEMU cross targets. Same reason as #3: `kunit.py` always builds out of tree.

---

## A test build comes from committed sources

`build_module.sh` does **not** copy sources in. It checks that the driver
committed on `feature/jockey3` matches this repository byte for byte, points
the build worktree at that commit, and builds what is there.

The reason is that a result has to name something. A module compiled from
whatever was lying in a build worktree cannot be reproduced, and the manifest
would name a revision that never contained those bytes. That is not a
hypothetical: before this rule, a module was built in a worktree that was both
modified and a commit behind its branch, and its manifest read `dirty: false`
because only the *repository* was checked.

So the loop before testing on hardware is:

```sh
# 1. commit in this repository, as usual
# 2. stage into the kernel branch and commit there
tests/build/sync-driver.sh ~/sound
git -C ~/sound add sound/usb/jockey3
git -C ~/sound commit --amend --no-edit     # feature/jockey3 is one patch
# 3. build and deploy
tests/build/build_module.sh x86_64-debug --manifest
```

`feature/jockey3` is a single squashed patch on `origin/for-next` — the one
that gets submitted — so step 2 is normally an amend rather than a new commit.

`--uncommitted` restores copy-then-build for quick iteration. It says so
loudly, and the manifest records `kernel_driver_dirty: true`, so such a build
stays distinguishable afterwards. It should not be what a recorded test result
came from.

The manifest carries both halves of the answer, because both can move
independently:

| Field | What it identifies |
|---|---|
| `git_hash`, `git_describe`, `dirty` | the driver source — this repository |
| `kernel_git_hash`, `kernel_git_describe`, `kernel_driver_dirty` | the tree that supplied headers, config and `Module.symvers` |
| `kernel_release`, `srcversion`, `build_id` | the binary itself |

## How source moves

One direction only, by copy, never by symlink:

```
dev sandbox  --(tests/build/sync-driver.sh)-->  ~/sound  and  ~/sound-build
```

`build_kernel.sh` still syncs (it builds whole kernels, where the driver is
one file among thousands); `build_module.sh` no longer does.

`sync-driver.sh` holds **the one list of driver source files**. Adding a new
source file means adding it there, or the build silently omits it.

It copies with `cp -u`, **deliberately never `cp -pu`**: preserving mtimes
makes the copy look older than the previous build's objects, so make skips the
rebuild and quietly revalidates the old revision. That has bitten this project
twice.

Nothing ever flows back. Edits made in a kernel tree are overwritten without
warning on the next sync.

---

## The tooling

| Tool | Runs on | What it does |
|---|---|---|
| `make` (repo root) | dev box | Out-of-tree compile check. Not loadable. |
| `tests/build/sync-driver.sh <tree>` | dev box | The file list. Copies sandbox → kernel tree. |
| `tests/build/build_jockey3.sh` | dev box | L1 gates in `~/sound`: checkpatch, `W=12`, kernel-doc, codespell, rst, module size. Produces a verdict. |
| `tests/build/build_kernel.sh <target>` | dev box | Builds a target kernel + `.deb`s: `~/sound-build` → `~/kbuild/<target>`. |
| `tests/build/build_module.sh <target>` | dev box | Builds a **loadable** module for a target, from the sources committed on `feature/jockey3`. Refuses if the branch does not match this repository, or if `vermagic` does not match the target. `--manifest` records build-id → git. |
| `tests/build/write-manifest.sh` | dev box | build-id → git revision, into the Seafile-synced manifests dir. |
| `tests/codec/run_kunit.sh` | dev box | KUnit under UML/QEMU, via `~/sound-kunit`. |
| `tests/codec/codecbench.py` | dev box | User-space codec correctness and benchmarking. |
| `tests/hw/priv/install.sh` | test machine | Installs the privileged helper + sudoers. Once per machine. |
| `tests/hw/actions/reload_driver.sh <ko>` | test machine | Deploys and reloads a module. Asks for a password by design. |
| `tests/hw/runner.py` | test machine | Runs a profile. Ordinary user. |

### Which build do I want?

| Question | Tool |
|---|---|
| Does it still compile? | `make` in the sandbox |
| Is it clean enough to submit? | `build_jockey3.sh` |
| I need a module to test on hardware | `build_module.sh <target> --manifest` |
| I need a whole kernel for a target | `build_kernel.sh <target>` |
| Is the codec still correct? | `run_kunit.sh` / `codecbench.py` |

---

## Gaps

Placeholders, so they are visible rather than rediscovered:

- **No tool installs a target kernel on a test machine.** `build_kernel.sh`
  produces `.deb`s in `~/kbuild`; getting them onto the machine and booted is
  manual `scp` + `dpkg -i` + reboot.
- **Headers are not installed on `alsa-test`.** `linux-image-7.2.0-rc5-alsa-debug+`
  is installed but the matching `linux-headers` package never was, so there is
  no `/lib/modules/$(uname -r)/build` and the module cannot be built on the
  test machine itself. Fine while the dev box builds everything; it becomes a
  real limitation for a Raspberry Pi target, where cross-building is the
  alternative.
- **No bootstrap for a new test machine.** Installing the helper, the tools and
  the right kernel is a checklist nobody has written down. `priv/install.sh`
  covers only the privilege part.
- **`i386-debug` and `armhf-debug` have no hardware target.** `tests/configs/`
  carries both configs for every architecture, but `tests/hw/targets.yaml`
  only lists `-prod` as an active target for `i386` and `armhf` — the debug
  pair exists for `check-debug-config.sh` parity and for L1/L2 builds, not
  because a debug kernel runs on that hardware.
- **`~/sound`'s config drifts.** Nothing keeps its `.config` in step with what
  it is used for, and `build_jockey3.sh` does not check. The gates do not
  depend on it, but the stale `kernelrelease` it produces is a standing trap
  for anyone who assumes a module built there is loadable.
