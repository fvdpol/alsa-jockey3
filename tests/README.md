# tests/

Test tooling for the Ploytec codec. The full guide is
**[../docs/codec_testing.md](../docs/codec_testing.md)**; this is just a map of the
directory.

| Path | What it is |
|---|---|
| `ploytec_model.py` | Independent model of the wire format, derived structurally rather than copied from the C. The oracle everything else is anchored to. |
| `genvectors.py` | Turns the model into `../ploytec_codec_test_vectors.h`, the golden vectors shared by the KUnit suite and the bench. |
| `run_kunit.sh` | Runs the in-kernel KUnit suite, under UML or cross-built for QEMU. |
| `codecbench.py` | Builds, validates and benchmarks the codec in user space. |
| `harness/` | The C bench: `main.c`, the implementation registry, and the kernel-header stubs that let driver source compile in user space. |
| `candidates/` | Experimental codec implementations. Drop a file in, rerun `./codecbench.py test`. |
| `build/` | Generated; git-ignored. Holds the verbatim copy of the driver codec plus its SHA-256 manifest. |

## The short version

```sh
./run_kunit.sh                 # KUnit under UML
./run_kunit.sh --list          # which cross targets are runnable

./codecbench.py test           # correctness, every implementation
./codecbench.py bench --quick  # fast, noisy benchmark
./codecbench.py bench          # 30s per measurement, trustworthy
./codecbench.py check-sync     # is build/ still the current driver code?
```

## Two things worth knowing

**The driver source is copied, not symlinked.** `codecbench.py build` takes a
verbatim copy into `build/src/` and records its SHA-256. `check-sync` fails if
the driver has moved on since, so a benchmark number cannot be attributed to
the wrong revision. Run it before quoting figures.

**Nothing here requires changes to the driver.** All three codec variants are
built from that single copy by compiling it three times with different
`CONFIG_*` defines and renaming its public symbols on the command line. The
driver has no exports, `#ifdef`s or hooks added for testing, and it must stay
that way.
