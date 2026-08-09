# tests/

Everything used to test this driver, in three parts.

| Path | What it is |
|---|---|
| `codec/` | Codec correctness and benchmarking. Runs anywhere, needs no hardware. Guide: **[../docs/codec_testing.md](../docs/codec_testing.md)** |
| `hw/` | The test runner, catalog and cases that exercise the driver against real hardware. Guide: **[../docs/test_strategy.md](../docs/test_strategy.md)** |
| `scripts-alsa-dev/` | Archive of the ad-hoc scripts this suite grew out of. Kept for reference; not maintained. |

---

## `codec/`

| Path | What it is |
|---|---|
| `ploytec_model.py` | Independent model of the wire format, derived structurally rather than copied from the C. The oracle everything else is anchored to. |
| `genvectors.py` | Turns the model into `../../ploytec_codec_test_vectors.h`, the golden vectors shared by the KUnit suite and the bench. |
| `run_kunit.sh` | Runs the in-kernel KUnit suite, under UML or cross-built for QEMU. |
| `codecbench.py` | Builds, validates and benchmarks the codec in user space. |
| `harness/` | The C bench: `main.c`, the implementation registry, and the kernel-header stubs that let driver source compile in user space. |
| `candidates/` | Experimental codec implementations. Drop a file in, rerun `./codecbench.py test`. |
| `build/` | Generated; git-ignored. Holds the verbatim copy of the driver codec plus its SHA-256 manifest. |

```sh
cd tests/codec
./run_kunit.sh                 # KUnit under UML
./run_kunit.sh --list          # which cross targets are runnable

./codecbench.py test           # correctness, every implementation
./codecbench.py bench --quick  # fast, noisy benchmark
./codecbench.py bench          # 30s per measurement, trustworthy
./codecbench.py check-sync     # is build/ still the current driver code?
```

### Two things worth knowing

**The driver source is copied, not symlinked.** `codecbench.py build` takes a
verbatim copy into `build/src/` and records its SHA-256. `check-sync` fails if
the driver has moved on since, so a benchmark number cannot be attributed to
the wrong revision. Run it before quoting figures.

**Nothing here requires changes to the driver.** All three codec variants are
built from that single copy by compiling it three times with different
`CONFIG_*` defines and renaming its public symbols on the command line. The
driver has no exports, `#ifdef`s or hooks added for testing, and it must stay
that way.

---

## `hw/`

| Path | What it is |
|---|---|
| `runner.py` | Coordinator. Runs a profile on this machine and writes a result record. |
| `catalog.yaml` | The register of every test case, implemented or not. |
| `targets.yaml` | The machines and kernel flavors tests run on. |
| `profiles.yaml` | Which cases run, how many times, with what parameters, per target. |
| `lib/` | Environment capture, dmesg classification, ALSA helpers, result schema. |
| `cases/` | One executable per automated case. |
| `actions/` | Small reusable steps (load the driver, play a tone, change rate). |
| `checklist.py` | Renders manual cases to a checklist and reads the answers back. |
| `ledger.py` | What has been tested, on what, how recently — and metric trends. |
| `selftest.py` | Tests for the framework itself. No hardware, no root. |
| `results/` | Generated; git-ignored. |

```sh
cd tests/hw
./runner.py --list                        # cases and profiles
./runner.py --profile smoke --dry-run     # what would run here
sudo ./runner.py --profile smoke          # run it
./ledger.py                               # coverage and staleness
./selftest.py                             # check the framework itself
```

The runner executes **on the machine with the hardware attached**, not on a
build host. That is what lets the same suite run on a Raspberry Pi you plugged
in five minutes ago.
