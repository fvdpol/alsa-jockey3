# Testing the Ploytec codec

This driver converts between ALSA's `S24_3LE` sample format and the Ploytec
wire format, in which the bits of each sample are scattered across many bytes.
The conversion runs on every USB packet in both directions, so it is the
hottest code in the driver — and it exists in three versions:

| Variant | Selected when | Implementation |
|---|---|---|
| Portable reference | `CONFIG_SND_USB_JOCKEY3_REFERENCE_CODEC=y` | straightforward bit-by-bit scatter/gather |
| 64-bit optimized | otherwise, if `CONFIG_64BIT` | 256-entry `u64` bit-spread table, magic-multiplier gather |
| 32-bit optimized | otherwise | two 256-entry `u32` tables, nibble gather |

Exactly one is compiled into any given build. The optimized ones are 4–10×
faster and are what every user actually runs, but they rely on SWAR tricks
(`0x8040201008040201` and friends) that nobody can verify by reading. That is
what all of this exists to check.

Two tools cover it:

- **KUnit** (`ploytec_codec_kunit.c`) proves the *compiled-in* variant correct,
  inside the kernel, on architectures you don't physically own. This ships
  upstream with the driver.
- **The user-space bench** (`tests/codec/codecbench.py`) compares *all three*
  variants against each other and against experimental candidates, and
  measures them. This stays out of tree.

Both consume the same golden vectors, generated from an independent model of
the format, so they cannot quietly disagree about what "correct" means.

---

## Quick reference

```sh
cd tests/codec

./run_kunit.sh                     # KUnit under UML, a few seconds
./run_kunit.sh --all               # um, i386, arm64, arm, riscv
./run_kunit.sh --list              # what's runnable, what's missing

./codecbench.py test               # correctness, all implementations
./codecbench.py bench              # benchmark, 30s per measurement
./codecbench.py check-sync         # is build/ still the current driver code?
```

---

## Why the tests are structured this way

A test is only worth the independence of its oracle. The obvious approach —
keep a copy of the original loops and check the optimized versions against it —
is weak, because the copy and the code under test embody the *same*
understanding of the format. If that understanding is wrong, both are wrong
together and the test passes.

So correctness rests on three independent legs:

1. **A structural model** (`tests/codec/ploytec_model.py`). It derives the bit
   mapping from a description of the format — channel pairing, plane ordering
   by significance, bit assignment within a wire byte — rather than
   transcribing the C. It is cross-checked against a literal transcription of
   the driver's reference loops; that agreement is itself a test, run by
   `python3 ploytec_model.py`.

2. **A declarative oracle** in the KUnit suite. The format is stated as a
   table of bit positions rather than as control flow:

   ```c
   static const struct ploytec_encode_map_entry ploytec_encode_map[] = {
           {  2,  0, 0 }, {  8,  0, 1 },   /* ch0 / ch2, most significant byte */
           {  1,  8, 0 }, {  7,  8, 1 },   /* ch0 / ch2, middle byte */
           ...
   ```

   Because it is a different *shape* from the driver's loops, a transcription
   slip cannot propagate symmetrically into both.

3. **Properties that hold regardless of implementation.** The codec is a pure
   bit permutation, so as a function over GF(2) it is *linear*. Two cases
   exploit that:

   - `*_permutation_complete` feeds a one-hot input for every single input bit
     and checks exactly one output bit is set, at the position the map
     predicts, with no two inputs colliding.
   - `*_is_linear` checks `f(a ^ b) == f(a) ^ f(b)`.

   A linear map is **fully determined by its action on the basis vectors**. So
   together these pin down the result for all 2⁹⁶ possible playback inputs,
   rather than sampling a few million of them.

   This is not a formal proof — linearity is itself established by sampling,
   so a non-linearity hiding outside the sampled pairs would slip through. But
   it is far stronger than random comparison.

### The two legs are not redundant

Injecting an adjacent bit-order swap into the 64-bit spread table produced:

```
[FAILED] ploytec_test_encode_permutation_complete
         src[0] bit 0 landed at output bit 176, map says 184
[PASSED] ploytec_test_encode_is_linear
[PASSED] ploytec_test_encode_zero_input
[PASSED] ploytec_test_encode_reserved_bits_clear
```

A permuted map is still linear, still maps silence to silence, and still
leaves the reserved bits clear. Only the position check against the
declarative map caught it — and it named the exact bit. Conversely, linearity
is what turns the basis enumeration into a statement about every input rather
than about 96 special cases.

Whenever you touch the tests, break the codec on purpose and confirm they
fail. A suite that has never failed has not been tested.

---

## KUnit

### Running

`run_kunit.sh` wraps `kunit.py`, knowing the cross-compiler and QEMU binary
each target needs.

```sh
cd tests/codec
./run_kunit.sh                  # UML only (default, fastest)
./run_kunit.sh arm64 riscv      # specific targets
./run_kunit.sh --all            # um i386 arm64 arm riscv
./run_kunit.sh --all --extra    # also s390, as a big-endian proof
./run_kunit.sh --ref um         # force the portable reference codec
./run_kunit.sh --list           # target/toolchain status
./run_kunit.sh --clean          # drop the build directories
```

`--list` reports precisely which Debian packages are missing per target:

```
TARGET   CROSS-COMPILER             QEMU                     STATUS
um       (host)                     (none, UML)              ready
i386     (host)                     qemu-system-i386         missing: qemu-system-x86
arm64    aarch64-linux-gnu-         qemu-system-aarch64      missing: qemu-system-arm
```

Everything needed for the full set:

```sh
sudo apt install qemu-system-x86 qemu-system-arm qemu-system-misc opensbi \
                 gcc-riscv64-linux-gnu
# for the optional big-endian run:
sudo apt install gcc-s390x-linux-gnu
```

> If a cross build fails with something like
> `ar: arch/x86/kernel/static_call.o: No such file or directory`, it is a
> parallel-make race in the kernel build, not a problem with the tests. Simply
> re-run — the second pass picks up where it left off.

### Which targets matter, and why

| Target | What it exercises | Status |
|---|---|---|
| `um` | The fast path for everyday work. x86_64, 64-bit codec. | 75/75 |
| `i386` | The `lut32`/`pack32` branch as the kernel really builds it. | 75/75 |
| `arm` | Strict alignment, 32-bit — the same shape as the Raspberry Pi 1B. | 75/75 |
| `arm64` | Reproducible arm64 coverage without plugging in the Pi 4. | 75/75 |
| `riscv` | Genuinely untested territory; no hardware here at all. | 75/75 |
| `s390` | Big-endian. Not a realistic platform for this device, but the only way to exercise the `get/put_unaligned_le*` paths in anger. Opt-in via `--extra`. | 75/75 |

### Two per-architecture config quirks

Both are handled by `run_kunit.sh`; they are recorded here because they are
non-obvious and cost time to rediscover.

Both are passed with `--kconfig_add` rather than placed in `.kunitconfig`,
because kunit.py treats a requested option it cannot satisfy as an **error** —
so an architecture-specific symbol in the shared fragment breaks configuration
on every *other* architecture. That is not hypothetical: it is exactly how the
first cross-architecture run failed.

**UML cannot reach `CONFIG_USB`.** It disables IOMEM by default, which makes
`USB_SUPPORT` — and with it all of `sound/usb` — unselectable. The only
user-selectable way in is `CONFIG_UML_PCI_OVER_VIRTIO`, which selects
`UML_PCI` and pulls in the IOMEM emulation. `VIRTIO_UML` is a hard dependency
of it:

```sh
--kconfig_add CONFIG_VIRTIO=y \
--kconfig_add CONFIG_VIRTIO_UML=y \
--kconfig_add CONFIG_UML_PCI_OVER_VIRTIO=y
```

**s390 needs `CONFIG_PCI=y`.** `arch/s390/Kconfig` has
`config HAS_IOMEM / def_bool PCI`, so without PCI there is no IOMEM, hence no
`SOUND` and no `USB_SUPPORT` — the same wall as UML, reached by a different
route.

### The separate worktree

`kunit.py` always builds out of tree, and the kernel refuses an out-of-tree
build when the source tree already holds an in-tree configuration — which
`~/sound` does, because `build_jockey3.sh` builds with `make M=…` there.

Rather than asking anyone to run `make mrproper` on a working tree,
`run_kunit.sh` keeps its own git worktree (`~/sound-kunit` by default),
syncs the driver sources into it, and builds there. `~/sound` is untouched.
To undo it entirely:

```sh
git -C ~/sound worktree remove ~/sound-kunit
```

Override the locations with `KERNEL_SRC`, `KUNIT_TREE` and `KUNIT_REF`.

> The sync deliberately uses plain `cp`, not `cp -p`. Preserving timestamps
> makes the copy older than the previous run's object files, so `make` skips
> the rebuild and silently retests the old revision. That bit once already.

### Testing both Kconfig paths

The reference codec sits behind `EXPERT`, so it is easy to leave untested:

```sh
./run_kunit.sh --ref um
```

`ploytec_test_codec_variant_matches_build` asserts the preprocessor chain
actually selected the variant the Kconfig asked for. That is not a
hypothetical failure mode — the previous user-space harness silently built the
32-bit codec on x86_64 for months.

### Adding a case

Add the function, then register it in `ploytec_codec_test_cases[]`. Use
`KUNIT_ASSERT_*` inside loops that would otherwise print thousands of
failures, `KUNIT_EXPECT_*` when you want the case to keep going. Randomized
cases use the seeded xorshift in the file rather than the kernel RNG, so every
architecture sees identical data and failures reproduce exactly.

---

## The user-space bench

### What it is for

1. **Correctness** of all three driver variants at once — including the two
   the current machine wouldn't normally compile.
2. **Benchmarks**, easily run on different hardware.
3. **A workbench** for developing new codec algorithms before any of them goes
   near the driver.

### How it gets the driver code

Every `build` copies `ploytec_codec.c` and `ploytec_codec.h` into
`build/src/` **verbatim** — never edited, never symlinked — and records a
SHA-256 manifest alongside:

```sh
./codecbench.py check-sync
# build/src is in sync with the driver (87ef17d)
```

`check-sync` exits non-zero if the driver has moved on, so a benchmark number
can never be attributed to the wrong revision. Run it before quoting figures
anywhere.

### How all three variants end up in one binary

The driver compiles exactly one variant, chosen by a preprocessor chain. To
get all three, the *same copied file* is compiled three times with different
`CONFIG_*` defines, and its three public symbols are renamed per variant on
the command line:

```
cc -DCONFIG_SND_USB_JOCKEY3_REFERENCE_CODEC=1 \
   -Dploytec_encode_batch=reference_ploytec_encode_batch  ...

cc -DCONFIG_64BIT=1 \
   -Dploytec_encode_batch=opt64_ploytec_encode_batch      ...

cc -Dploytec_encode_batch=opt32_ploytec_encode_batch      ...
```

The driver gains no exports, no `#ifdef`s and no test hooks. A
`kbuild_shim.h` carrying the kernel's real `IS_ENABLED()` machinery is
force-included exactly as kbuild does, so the selection behaves as it does in
the kernel.

You can confirm the branches really differ:

```sh
size build/ploytec_codec_*.o
#   text   data    bss  filename
#    967      0      0  reference   <- no lookup table
#   1081      0   2048  opt64       <- one 2 KB u64 table
#   1729      0   2048  opt32       <- two 1 KB u32 tables
```

Both optimized variants are *correct* on any host — `opt64` on a 32-bit
machine is merely slow — so all three are always available for cross-checking,
even where only one is the real build.

### Correctness

```sh
./codecbench.py test
./codecbench.py test --only opt64
```

Three layers, applied identically to driver variants and candidates:

- against the golden vectors from the Python model (the only layer that can
  catch the whole family being wrong together);
- against the driver's own portable reference over random frames;
- the property tests described above, mirroring the KUnit suite.

### Benchmarking

```sh
./codecbench.py bench --quick                 # 1s per measurement, smoke check
./codecbench.py bench                         # 30s per measurement (default)
./codecbench.py bench --duration 5m --repeats 5   # publication quality
./codecbench.py bench --json results-$(hostname).json
```

Measurements go through the **batch API at the driver's real batch sizes** (10
frames encode, 8 decode), which is what `jockey3_process_out_packet()`
actually calls — the old harness only ever measured single frames, so batching
effects were invisible. Timing uses `CLOCK_MONOTONIC`, with a warm-up pass, a
calibration burst to size the run, and a clobber barrier rather than the old
trick of feeding output back into the input (which changed the data being
measured).

**Choosing `--duration`.** Short runs are dominated by scheduler and thermal
noise. The tool reports relative standard deviation across repeats and flags
anything above 5%:

```
IMPLEMENTATION     KIND       ENC ns/frame DEC ns/frame  ENC RSD  DEC RSD
reference          driver            65.17       120.33    23.8%     8.4%  <- noisy, raise --duration
opt64              driver             4.62        12.85     0.8%     2.3%
opt32              driver             8.49        19.90     0.7%    25.1%  <- noisy, raise --duration
```

That is a `--quick` run: usable as a smoke test, useless as a number. Raise
`--duration` until the RSD settles. 30s is a reasonable default; 5m is
appropriate for figures you intend to publish or paste into the driver's
performance table.

The tool prints an estimated total run time up front, because
`--duration 5m` across four implementations and two directions is not a
short wait — and on a Raspberry Pi 1B it is a very long one.

### Cross-architecture runs

```sh
./codecbench.py --cross aarch64-linux-gnu --run-under qemu-aarch64-static test
./codecbench.py --cross arm-linux-gnueabihf --static build   # copy to the Pi
```

**QEMU timings are meaningless.** Emulation is for *correctness* on foreign
architectures; the tool says so when you use `--run-under`. Benchmark numbers
must come from native runs on real hardware.

### Merging results

```sh
./codecbench.py report results-*.json
```

Produces a markdown table with per-machine speedups relative to that machine's
own portable reference — which is how the performance table in the header of
`ploytec_codec.c` gets maintained.

---

## Developing a new codec: the promotion path

`candidates/` is where a new algorithm lives until it has earned its way into
the driver.

**1. Write it.** A file `candidates/foo.c` defines three functions named after
itself:

```c
#include "candidate.h"

void foo_init(void);                                                /* may be empty */
void foo_encode_batch(u8 *dest, const u8 *src, const int n_frames);
void foo_decode_batch(u8 *dest, const u8 *src, const int n_frames);
```

`codecbench.py` finds it by filename and generates the registry entry. There
is nothing to register by hand and no C file to edit.

If your algorithm genuinely works one frame at a time,
`PLOYTEC_CANDIDATE_FROM_FRAME()` writes the batch wrappers for you. Prefer the
batch form when you can: the driver always passes 10 or 8 frames at once, so
there is room to reuse loaded words, unroll across frame boundaries, or go
wider with SIMD — an avenue a per-frame interface hides entirely.

**2. Prove it.**

```sh
./codecbench.py test --only foo
```

It faces the same three layers as the shipped code. No exceptions: an
implementation that is fast and subtly wrong is worse than the one it replaces.

**3. Measure it, natively, on each machine that matters.**

```sh
./codecbench.py bench --duration 30s --json results-$(hostname).json
```

Watch the RSD. The 32-bit and 64-bit variants do not rank the same everywhere —
on the Raspberry Pi 1B the 64-bit spread table still won for encode while the
32-bit gather won for decode, which is exactly why the driver picks by
`CONFIG_64BIT` rather than assuming.

**4. Promote it.** Move the implementation into `ploytec_codec.c` inside the
appropriate branch of the preprocessor chain, wire it into the binding block
at the bottom of the file, and add a KUnit run for the configuration it
affects. Then re-run everything:

```sh
./codecbench.py check-sync && ./codecbench.py test
./run_kunit.sh --all
cd ~/sound && ./build_jockey3.sh
```

Keep the candidate in `candidates/` afterwards. It costs nothing and it means
the next algorithm has something to be measured against.

---

## Regenerating the golden vectors

`ploytec_codec_test_vectors.h` is generated and committed, because the kernel
tree cannot run the generator:

```sh
cd tests/codec
./genvectors.py
git diff ../../ploytec_codec_test_vectors.h
```

Regenerate only when `ploytec_model.py` changes. A diff appearing when you did
not expect one means the model changed meaning — investigate before committing.

Alongside random frames, the vectors pin down specific format claims:
`unused_bytes_only` must decode to silence, `unused_bits_only` likewise, an
all-ones playback frame must encode to `0x03` bytes (only two channels per
wire byte), and `group0_only` must yield channels 0, 2 and 4.
