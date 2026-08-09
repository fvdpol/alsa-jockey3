# tests/configs/

Kernel configurations for the test targets. One file per target, named exactly
as the target is: `x86_64-debug.config` builds `x86_64-debug`.

These exist so a target means the same thing every time it is built. A result
recorded against `x86_64-debug` is only comparable with other `x86_64-debug`
results if that name refers to the same set of options — otherwise the metric
series quietly measures the configuration rather than the driver.

| File | Target |
|---|---|
| `x86_64-debug.config` | `x86_64-debug` — KASAN, lockdep, kmemleak, codec KUnit at module load |
| `x86_64-prod.config` | `x86_64-prod` — derived from the above, debugging off |
| `derive-prod.sh` | Regenerates a `-prod` config from the matching `-debug` one |

Configs for `arm64-*`, `armhf-prod` and `i386-prod` are not here yet.

## Each config names its own target

Every config sets `CONFIG_LOCALVERSION` to `-alsa-debug` or `-alsa-prod`, so
`uname -r` identifies what is running and the test runner needs no help:

```
7.2.0-rc5-alsa-debug   ->  x86_64-debug
```

It is set in the config rather than passed as `LOCALVERSION=` on the make
command line, so it travels with the configuration and survives
`make bindeb-pkg` for the Pis. See `../../docs/test_strategy.md` §4.

## Building a target kernel

```sh
cp tests/configs/x86_64-debug.config ~/sound/.config
make -C ~/sound olddefconfig
make -C ~/sound -j$(nproc)
```

## Refreshing for a newer kernel

`olddefconfig` in place — no copying, no chance of the tree's `.config`
becoming the source of truth by accident:

```sh
make -C ~/sound olddefconfig \
     KCONFIG_CONFIG=$PWD/tests/configs/x86_64-debug.config
./tests/configs/derive-prod.sh x86_64        # keep prod in step
git diff tests/configs/                      # review what the rebase changed
```

Review that diff rather than committing it blind: a new kernel can quietly
enable something that changes what the target measures.

## Production is derived, never hand-maintained

`derive-prod.sh` produces the `-prod` config from the `-debug` one by turning
the debugging off. Two independently maintained configurations drift, and once
they drift the difference between a prod and a debug result stops being "the
debug options" and starts being "who knows". The script is idempotent — run it
after any change to the debug config.

One trap it encodes: disabling `PROVE_LOCKING` alone does **not** disable
`LOCKDEP`, because `DEBUG_LOCK_ALLOC`, `DEBUG_RT_MUTEXES` and
`DEBUG_WW_MUTEX_SLOWPATH` also select it. A "production" kernel with lock
tracking still active would distort exactly the timing numbers that
configuration exists to produce.

## What stays enabled in production

These live under `DEBUG_KERNEL` but are kept in both configurations, because
the test framework depends on them rather than merely benefiting from them:

| Option | Without it |
|---|---|
| `DYNAMIC_DEBUG`, `DEBUG_FS` | The firmware revision is only ever a `dev_dbg` at probe; every run records "firmware unknown". |
| `IKCONFIG_PROC` | No `/proc/config.gz`, so a run cannot check its own target label. |
| `DETECT_HUNG_TASK` | `INFO: task ... blocked` can never be emitted, so a stuck ioctl looks like a slow one. |
| `MAGIC_SYSRQ`, `KALLSYMS_ALL` | No way to get a symbolized task dump out of a wedged machine over the serial console. |
| `WQ_WATCHDOG` | A stalled workqueue in the reset path is indistinguishable from a slow reset. |
| `SND_PCM_XRUN_DEBUG` | Xrun counts are a metric here, not a debug aid. |

`MODULE_SIG_FORCE` must stay **off** in both, or locally built modules will not
load at all.
