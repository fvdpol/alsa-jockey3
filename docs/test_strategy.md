# Test strategy

This document describes how the Reloop Jockey 3 driver is tested: what is
covered, on which machines, how often, and how results are recorded.

It exists because testing had become the weakest-documented part of an
otherwise carefully documented project. The codec is rigorously validated
(see **[codec_testing.md](codec_testing.md)**), but everything above it — PCM
lifecycle, URB rings, rate switching, MIDI, power management, hotplug — was
exercised by hand, with a growing pile of one-off shell scripts and no record
of what had been run against which build. That is fine while a driver is being
brought up. It stops being fine once regressions start costing a bisect.

---

## 1. Purpose and scope

The goal is **consistency and repeatability**, not certification.

- Catch regressions during development, without bisecting back through many
  commits to find where behavior changed.
- Make it cheap to re-run a focused subset after a change, and a fuller pass
  overnight.
- Make coverage legible: what has been tested, on what hardware, how recently,
  and — just as important — what has *not*.
- Support an honest summary of testing in the mainline submission.

Deliberately **out of scope**:

- **A certification artifact.** Nobody is being handed a signed test report.
- **Per-control firmware validation.** The device has dozens of buttons, knobs,
  faders and two jog wheels. Verifying each one individually tests Reloop's
  firmware, not this driver. If the driver correctly delivers *a* button press
  and *a* jog movement, it delivers all of them — the code path does not know
  which control moved.
- **Audio quality metrology.** THD, frequency response and noise floor are
  properties of the hardware. The driver's job is to move bits without
  corrupting or dropping them. Corruption is audible, and a human ear catches
  it faster and more reliably than a measurement rig would.
- **The Master Edition (`200c:1009`).** It is in the device table but no
  hardware is available to test it. This is stated plainly in the README rather
  than papered over, and depends on user feedback.
- **An ALM-style tracking system.** Test IDs and a results file, nothing more.

---

## 2. What is being tested

The driver's externally visible surface, and therefore the thing tests must
cover:

| Area | Surface |
|---|---|
| Enumeration | Two device IDs; binds interface 0 and claims interface 1; validates three bulk endpoints (`0x05` OUT playback+MIDI, `0x86` IN capture, `0x83` IN MIDI) |
| PCM | One device, one playback substream (4 channels) and one capture substream (6 channels); `S24_3LE` only; 44100 / 48000 / 88200 / 96000 Hz |
| PCM limits | `period_bytes_min` 120 playback / 144 capture, `period_bytes_max` 512 KiB, `buffer_bytes_max` 1 MiB, 2–1024 periods, integer period count |
| Controls | Channel maps only. Playback `FL, FR, UNKNOWN, UNKNOWN` (Master L/R, Headphone L/R); capture all `UNKNOWN` (In1 L/R, In2 L/R, Mic duplicated across 5/6). No volume or routing controls exist. |
| MIDI | One rawmidi device, duplex. The entire control surface arrives as raw MIDI; LEDs are driven by MIDI OUT. |
| Power | `suspend`, `resume`, `reset_resume`, `pre_reset`, `post_reset`. `SNDRV_PCM_INFO_RESUME` is deliberately *not* advertised. |

### Where the risk actually is

Tests are worth writing where failure is plausible. For this driver that is:

- **Capture stall after a sample-rate change.** Known, documented, and
  *mitigated rather than root-caused*. The endpoint stops delivering after a
  rate change — worst on a downward switch — despite the control transfer
  reporting success. Recovery escalates from a lightweight URB restart to a
  full USB reset. This is the single most important area to test, and it has
  the most counter-intuitive pass criterion (see §9).
- **URB lifecycle.** The `callbacks_active` safe-zone counter, `sync_stop`
  draining with a 1000 ms cap, the `stopping` flag checked inside the same
  critical section that resubmits. Use-after-free lives here.
- **Lock ordering.** `snd_pcm_stream_lock` → `urb_stream->lock` is mandated;
  completion handlers must drop their spinlock before calling
  `snd_pcm_period_elapsed()` or `snd_pcm_stop_xrun()`. An ABBA inversion here
  was a real historical bug.
- **The reset path.** `usb_reset_device()` must never be called synchronously
  from an ALSA ioctl — it can deadlock against `snd_card_free()` in the same
  thread. Hence `usb_queue_reset_device()` plus a polled wait. `post_reset()`
  is not guaranteed to run, so `disconnect()` also completes the waiters.
- **Error storms.** Eight consecutive URB errors per direction stops
  resubmission and defers recovery. On unplug all eight in-flight URBs fail at
  once, so the give-up path is entered routinely, not exceptionally.
- **MIDI running status.** The device does not accept running status, so the
  driver expands it. One byte per 512-byte packet, rate-limited to 3125 B/s by
  a leaky bucket whose divisor is derived from the sample rate — meaning MIDI
  throughput must be re-verified at *each* rate.
- **Suspend.** The device loses stream sync, so `RESUME` is not advertised and
  applications are expected to fail with `-ESTRPIPE` and re-prepare.

---

## 3. Test levels

Levels describe **the scope of what is exercised**, and nothing else. Whether a
case is automated or performed by a human is a separate, orthogonal property —
L3 contains both a scripted channel sweep and a human LED check.

| Level | Scope | Needs hardware? |
|---|---|---|
| **L1 — Static and build** | The source as text and as a compilation unit: checkpatch, `W=12`, kernel-doc, codespell, rst, cross-compilation for every target architecture. | No |
| **L2 — Component** | One component in isolation, no kernel running the real thing: the codec under KUnit across six architectures and both variants, against golden vectors and an independent model. | No |
| **L3 — Driver function** | One driver behavior at a time against a real device: probe, bind/unbind, PCM open/prepare/trigger, rate switching, MIDI in and out, LEDs, suspend/resume, hotplug. | Yes |
| **L4 — Integration** | The driver inside a real audio stack under a real workload: Mixxx, full duplex, simultaneous MIDI and audio, PipeWire and desktop interaction. | Yes |
| **L5 — Endurance** | Time and repetition: multi-hour playback, thousands of rate changes, repeated bind/unbind, power-cycle loops. | Yes |

L1 and L2 are already realized — by `build_jockey3.sh` and by the KUnit suite
and bench respectively. This strategy is mostly about giving L3–L5 the same
treatment.

---

## 4. Targets

A **target** is `<architecture>-<kernelconfig>` — an architecture paired with a
named kernel configuration. A debug kernel is a genuinely different test
environment: KASAN and lockdep change timing enough both to expose races that
production builds hide and to mask races that only appear at full speed.

**A target describes what was built, not which box it ran on.** Which EliteDesk
it was, how much memory it had, what the governor was doing — that is
environment context, recorded with every run and never part of the target's
identity. The same `x86_64-debug` kernel gives the same target whichever
machine boots it, and the runner can be launched from any of them.

| Target | Typical machine | Role |
|---|---|---|
| `x86_64-debug` | HP EliteDesk 800 G2 (i5, 64 GB, NVMe) | Primary. Memory errors and lock inversions surface here or nowhere. |
| `x86_64-prod` | Same class of machine | Realistic timing, latency figures, long soaks. |
| `arm64-prod` | Raspberry Pi 4B, 2 GB | Second architecture, and a different USB host controller (dwc2, not xhci) — a behavioral difference, not a detail. |
| `arm64-debug` | Raspberry Pi 4B, 2 GB | Lock and memory checking on dwc2. Slow, so reserved for URB-lifetime and reset changes. |
| `armhf-prod` | Raspberry Pi 1B, 512 MB | 32-bit codec path, uniprocessor, no preemption. Sleeping in atomic context is fatal here and benign elsewhere. Slow; kernel switching is painful. |
| `i386-prod` | Not yet set up — planned via multi-boot on the EliteDesk | The 32-bit codec on a fast machine. Currently covered only by L1/L2. |

### The kernel names its own target

Each configuration is built with a distinctive `LOCALVERSION`, so `uname -r`
is enough to know what is running:

```
7.2.0-rc5-alsa-debug   ->  x86_64-debug
6.12.0-alsa-prod       ->  arm64-prod     (on aarch64)
```

Set it as `CONFIG_LOCALVERSION="-alsa-debug"` in the configuration rather than
passing `LOCALVERSION=` on the make command line: in the config it travels with
the configuration, survives `make bindeb-pkg` for the Pis, and appears in
`uname -r` on the booted machine. Matching is by substring, so the trailing `+`
that `setlocalversion` adds to an untagged git tree is harmless.

Probing for KASAN instead would be guesswork — it cannot distinguish a
deliberate debug kernel from a distribution kernel that happens to enable
lockdep, and it says nothing about the many other options that separate these
configurations. The debug options are therefore used only as a **consistency
check**: a kernel calling itself `-alsa-prod` with KASAN compiled in is a
mislabeled build, and the runner says so rather than quietly folding its timing
numbers into the production series.

A kernel that names no target is an error, not a default. Guessing is how a
run gets attributed to the wrong target, and a corrupted metric series is worse
than a missing one.

### What the kernel configurations need

Options the test framework depends on. Without these it does not fail loudly —
it quietly records less:

| Option | Why |
|---|---|
| `DYNAMIC_DEBUG=y`, `DEBUG_FS=y` | The firmware revision exists only as a `dev_dbg` at probe. Without dynamic debug it is never emitted and every run records "firmware unknown". |
| `IKCONFIG=y`, `IKCONFIG_PROC=y` | `/proc/config.gz`, so the target label can be cross-checked against the actual configuration. |
| `DETECT_HUNG_TASK=y` | The classifier treats `INFO: task ... blocked` as a defect. Without the detector that message can never appear, and a stuck ioctl looks like a slow one. |
| `MAGIC_SYSRQ=y`, `KALLSYMS_ALL=y` | A wedged machine can still be made to dump task state over the serial console, with readable symbols. |
| `MODULE_SIG_FORCE` **not set** | Otherwise locally built modules will not load at all. |

For the **debug** configuration, additionally:

| Option | Why |
|---|---|
| `KASAN`, `PROVE_LOCKING`, `DEBUG_ATOMIC_SLEEP`, `DEBUG_OBJECTS`, `DEBUG_LIST`, `SND_PCM_XRUN_DEBUG` | The bug classes this driver has actually had: use-after-free in URB teardown, ABBA lock inversion, sleeping in atomic context. |
| `KUNIT=y` + `SND_USB_JOCKEY3_CODEC_KUNIT_TEST=y` | Runs the 75 codec tests **at module load on the target itself**. Otherwise the 32-bit and arm codec paths are only ever validated under QEMU. |
| `WQ_WATCHDOG=y` | `usb_queue_reset_device()` runs on a workqueue and the driver waits up to a second for it. A stalled workqueue is a plausible failure here and otherwise looks like a timeout. |
| `DEBUG_KMEMLEAK` (optional) | Directly targets the leak class the bind/unbind stress exists to catch, now that devres was replaced with manual `devm` actions. Costs speed. |

Enabling the KUnit option changes the module's size, so `JT-BUILD-004` figures
are only comparable within a configuration — which is what per-target metric
series already gives. Do not enable it in the production configuration; its
own help text says as much.

### Build-only runs identify themselves from the tree

Running the L1 static gates on the build host is not testing the build host: it
is testing the configuration in `~/sound`, which may well be for another
architecture entirely. So a profile containing only L1/L2 cases takes its
target from the kernel tree's `.config` and `make kernelrelease`, not from
`uname`. The `build` profile exists for exactly this.

Machines are powered on and connected on demand, not permanently available.

**Devices under test:** two Reloop Jockey 3 Remix units, firmware 1.0.3 and
1.0.6. Firmware revision is **recorded, not varied deliberately** — both
revisions must work, and the driver already handles the known difference (which
padding bytes the device emits on MIDI IN). Upgrading 1.0.3 is not possible:
the vendor updater runs only on old macOS versions.

Only one device is wired to monitoring at a time.

---

## 5. Profiles

A **profile** is a named selection of cases with per-target iteration counts and
parameters. There is one definition of `smoke` — not a separate `pi1b-smoke` —
with a table saying what `smoke` means on each target.

| Profile | Intent | Typical duration |
|---|---|---|
| `smoke` | Does this build work at all? Run after every deploy. | minutes |
| `functional` | Each behavior exercised once. Run before calling a change done. | ~30 min |
| `regression` | Everything, with enough iterations to catch intermittent faults. | a few hours |
| `soak` | Endurance only. Run overnight. | 8+ hours |

Per-target overrides carry **iterations and parameters**, because scaling down
a test is not always a matter of running it fewer times. On the Pi 1B the
audio cases run at 44.1 and 48 kHz only; the high rates are skipped, not
merely reduced. An iteration count of `0` disables a case on that target.

This is what keeps effort proportionate: `x86_64-debug` earns thousands of rate
changes, the Pi 1B earns a bounded twenty-minute pass that answers "does it
load, play, capture, respond to MIDI, and survive a power cycle?"

---

## 6. Test identification

Every case has a stable ID: **`JT-<AREA>-<nnn>`**.

`BUILD` · `CODEC` · `PROBE` · `PCM` · `RATE` · `AUDIO` · `MIDI` · `PM` ·
`HOTPLUG` · `SOAK`

IDs never encode the level or whether the case is automated — both of those
change over a case's life, and an ID that changes is worthless. `JT-MIDI-002`
is the LED check whether a human watches the LEDs or a photodiode ever does.

All cases live in `tests/hw/catalog.yaml`, including ones not yet implemented,
marked `status: planned`. A catalog that only lists what already works cannot
show a gap, and showing gaps is half the point.

---

## 7. Execution model

**The runner executes on the machine with the hardware attached.** Not over
ssh from a build host. That is what makes it possible to plug in a Raspberry
Pi, copy the repository across, and run the same suite — no network topology
assumptions, no orchestration to debug when the interesting failure is a kernel
oops.

```
tests/hw/runner.py --profile smoke
```

Python coordinates; small bash scripts in `actions/` do the concrete work
(load the driver, play a tone, change the rate). Bash is kept for things bash
is good at — a handful of `aplay` and `amidi` invocations — and abandoned the
moment a case needs to parse or correlate anything, because non-trivial bash
becomes unmaintainable faster than any other language in common use. Cases may
be written in either; the runner spawns a subprocess and does not care.

Manual cases live in the same catalog and the same profiles. `checklist.py`
renders them into a markdown checklist for a given profile and target; the
answers are read back into the same result record, so one run produces one
report regardless of who or what performed each case.

---

## 8. Environment capture

A result is meaningless without knowing what produced it.

**Which driver binary.** The module's **GNU build-id**, read back from
`/sys/module/snd_reloop_jockey3/notes/.note.gnu.build-id`. It changes with
every build, so it identifies exactly what is loaded — including catching the
case where a reload silently failed and the previous build is still resident.
The build step writes a manifest keyed by build-id recording git hash, branch,
dirty flag, kernel release, architecture and configuration, which the runner
looks up.

The obvious candidate was `srcversion`, and it was the original design. It does
not work here: `/sys/module/<m>/srcversion` only exists when the kernel was
built with `CONFIG_MODULE_SRCVERSION_ALL`, which neither Debian nor the
Raspberry Pi kernels set — so it is absent on precisely the machines this has
to work on. The build-id is written by the linker regardless of kernel
configuration, and the kernel exposes module ELF notes in sysfs on every
supported architecture. It is recorded alongside `srcversion` and `vermagic`,
which are kept when present but never relied on.

This requires **no changes to the driver**. That constraint is absolute: the
driver ships upstream, and it must contain nothing that exists for the benefit
of tests — no exports, no hooks, no `#ifdef`s, no `MODULE_VERSION` carrying a
build-time git hash.

**Which firmware.** The revision is not in `bcdDevice`. It is read at probe by
`ploytec_get_firmware()` and surfaced only as a `dev_dbg`:

```
Firmware 0x31 v1.0.6
```

So the runner enables dynamic debug for that format string *before* loading the
module, and parses the message. Slightly indirect, but it needs no driver
change and observes the actual bound device.

**Which machine.** Context, not identity — but the context is what makes a
surprising number explicable. CPU model and core count, RAM, board model,
hostname, kernel release, distribution, ALSA versions — and two that are easy
to omit and expensive to omit:

- **The USB host controller driver.** xhci on the EliteDesk, dwc2 on the Pi 4.
  These behave differently under load and on disconnect.
- **The cpufreq governor and current frequency.** Latency and jitter numbers
  cannot be compared between runs without it.

**Which target.** Derived from the kernel's `LOCALVERSION` as described in §4,
and cross-checked against `/proc/config.gz` or `/boot/config-*` — never
declared by the operator, because the operator is the least reliable part of
the system at 2am.

**Preparation.** PipeWire/wireplumber is stopped so the device is not claimed,
`kernel.printk` is raised, and the card is resolved by driver match. All of it
is restored afterwards.

**Crash capture.** The EliteDesk's serial console is captured externally, which
survives the hang that makes dmesg unreachable. Moving that capture from a
laptop terminal to a networked logger is on the roadmap; until then, an
overnight crash is triaged by hand in the morning.

---

## 9. Result classification and pass criteria

### "No warnings" is not a pass criterion

Capture stall after a rate change is **expected behavior on a healthy build**.
The driver logs it deliberately, on every occurrence, precisely so that stall
frequency can be tracked. A test that failed on the presence of a warning would
fail every time on a perfectly good driver.

So `JT-RATE-*` cases distinguish:

- stalled and recovered → **pass**, with the stall counted as a metric
- stalled and still stuck after full reset → **fail**

### Four buckets

Every kernel message emitted during a case is classified:

| Bucket | Meaning | Effect |
|---|---|---|
| Expected, ours | From the driver, matching this case's expected patterns | Informational |
| Unexpected, ours | From the driver, not expected here | **Fail** |
| Known unrelated | Matches the global allowlist (other subsystems, boot noise) | Ignored |
| Unclassified | Everything else | **Flagged for review** |

The fourth bucket is the one that earns its keep. Rather than choosing between
a noisy allowlist and silently discarding unknown messages, unclassified lines
are surfaced for a human to look at once, and then either added to the
allowlist or turned into a real finding.

### Escalation

Some messages are not test failures but defects:

```
BUG:   WARNING:   Call Trace:   KASAN:   possible circular locking
Inconsistent URB in-flight count
Timeout draining ... URB callbacks
```

These mark the run `INVESTIGATE` and abort the remaining profile. The machine
will need manual intervention anyway, and the correct response is to open an
issue and look at it — not to retry, and not to record a failure count and
carry on.

---

## 10. Metrics and trends

Cases record numbers alongside their verdict. A boolean tells you something
broke; a number tells you it is *breaking*, three weeks before it does.

| Level | Metrics |
|---|---|
| L1 BUILD | `.ko` size, `size(1)` text/data/bss; checkpatch and `W=12` warning counts |
| L2 CODEC | encode/decode throughput and speedup over the reference |
| L3 PROBE | probe duration; module load and unload time |
| L3 PCM | xrun count; time to first audio; smallest period size that runs clean — i.e. achievable latency |
| L3 RATE | reset-completion delay histogram, stall rate per 1000 rate changes, how often recovery escalated to a full USB reset |
| L3 PM | suspend → resume → audio-restored duration |
| L3 MIDI | sustained and peak RX/TX byte rates |
| L5 SOAK | xruns per hour, memory growth, and drift in all of the above across the run |

Two reference points already exist and should be preserved as baselines: the
rate-change delay distribution captured in
`tests/scripts-alsa-dev/rate_change_histogram.txt`, and the Windows vendor
driver's MIDI throughput (RX peak 1695 B/s; TX 330 B/s sustained, 862 B/s peak)
recorded in `tests/scripts-alsa-dev/miditest/README.md`.

**Metrics are only comparable within a target with a matching machine profile.**
A 3 ms regression on the Pi 1B and on the EliteDesk are not the same
observation, and a governor change invalidates a latency series.

---

## 11. Results, retention and reporting

```
<results-root>/<target>/<UTC-timestamp>-<profile>/
    run.json        environment, driver identity, per-case results and metrics
    dmesg.txt       full capture
    cases/          per-case logs, recordings
```

`run.json` is the record. Everything beside it is evidence, kept locally in
date-stamped directories so old runs can be purged wholesale without thought.
Captured audio and full kernel logs do not belong in git.

The results root defaults into the Seafile tree (override with
`JOCKEY3_RESULTS_DIR`) so it is shared between the test machines without
needing git on each of them. Summaries are published to the repository by hand;
as the flow settles, more can move to git.

`ledger.py` answers the question that motivates all of this:

```
target          case          last pass    driver     commits since
x86_64-debug    JT-RATE-001   2026-08-06   a1b2c3d    3
arm64-prod      JT-MIDI-002   2026-07-21   9f8e7d6    41
armhf-prod      JT-PCM-002    never        —          —
```

A pass from three weeks ago still means something; it just means less. Showing
the age and the distance rather than a green tick keeps old data useful without
letting it masquerade as current. Whole-repository commit distance is used
deliberately — attributing relevance per source file would be false precision
in a driver where nearly every change touches `jockey3.c`.

---

## 12. Roadmap

**Now:** the catalog, the runner, and a small set of automated cases proving
the plumbing end to end.

**Next, in rough priority order:**

1. **Audio loopback** — master out into In 1, headphone out into In 2, using the
   device's own I/O. An RMS check per channel confirms audio is actually being
   produced on the expected channel at each rate, and validates the playback
   channel map. No extra hardware needed.
2. **Non-interactive MIDI flood** — `miditest.py` already measures throughput
   and running-status handling; it needs a scripted mode.
3. **Networked serial console logger** — so an overnight crash is captured to a
   file rather than into a terminal on a laptop that has since suspended.
4. **Mains relay (Sonoff/MQTT)** — power-cycling under program control. This is
   the only way to reach the give-up path where the driver reports that the
   hardware needs a power cycle, and it makes unattended recovery possible when
   the device wedges.
5. **USB port switching (`uhubctl`)** — surprise disconnect and replug loops,
   far faster than mains cycling, exercising card slot reuse and disconnect
   races.
6. **i386 bring-up** — multi-boot on the EliteDesk.
7. **Jitter measurement** — period-interval distribution needs more than `aplay`
   exposes; deferred until the framework is proven.

---

## 13. Relationship to the submission

Mainline readiness is tracked as a separate sequence of phases, of which this
work is Phase 7 (Phase 6 was the KUnit codec validation described in
[codec_testing.md](codec_testing.md)).

The upstream documentation deliberately does **not** reproduce any of this. A
detailed test plan in `Documentation/sound/cards/jockey3.rst` would be a second
copy to keep synchronized, and reviewers do not need it. The `.rst` states what
was tested and points here; the cover letter says which hardware and
architectures were used, and what kind of testing was done. That is an honest
account, and it is all that is warranted.
