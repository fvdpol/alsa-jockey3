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
- **The Master Edition (`200c:1019`).** It is in the device table but no
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
  rate change despite the control transfer reporting success. Recovery
  escalates from a lightweight URB restart to a full USB reset. This is the
  single most important area to test, and it has the most counter-intuitive
  pass criterion (see §9). The headline figure is `resets_per_change_pct` —
  how often a rate change costs a USB device reset — which must trend to
  zero. Measured incidence, the direction dependence, the fix that drove it
  to zero, and a later post-fix EP0 failure are all tracked in the working
  document, **[`re/rate_change_stall.md`](../re/rate_change_stall.md)**.
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
case is automated, needs a person mid-test, or is performed entirely by hand is
a separate, orthogonal property — L3 contains both a scripted channel sweep and
a human LED check. That property is the case's *mode*; see §7.

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

### A debug kernel cannot judge audio quality

Worth stating plainly because the evidence is counter-intuitive: the same
audio plays with a light continuous crackle on `x86_64-debug` and cleanly on
`x86_64-prod`, while every ALSA counter (`xrun_counter`, `avail_max`) reads
identically clean on both. The shortfall is entirely downstream of ALSA, in
the free-running URB ring, and is invisible to `/proc/asound`. See
**[`re/debug_kernel_audio_quality.md`](../re/debug_kernel_audio_quality.md)**
for the measurement and why the instrumentation cannot see it.

The consequence for the suite is narrow but firm:

- **Functional verdicts stay valid on a debug kernel** — does it enumerate,
  play, capture, switch rate, recover from a stall, survive a reload. These
  are what the debug target exists for, and KASAN and lockdep are the reason
  to run them there.
- **Verdicts about how it sounds are void.** `JT-AUDIO-003` and `JT-INTEROP-004`
  are disabled on the debug targets. Running them would record a failure
  against the driver for the kernel's overhead, which is worse than not
  running them at all.
- **`JT-AUDIO-001` stays enabled.** It asks which output a tone came from and
  at what pitch; a crackle does not make either ambiguous.

A latency or throughput figure from a debug kernel is likewise a fact about
KASAN, which is why §4 already puts those on `x86_64-prod`.

| Target | Typical machine | Role |
|---|---|---|
| `x86_64-debug` | HP EliteDesk 800 G2 (i5, 64 GB, NVMe) | Primary. Memory errors and lock inversions surface here or nowhere. |
| `x86_64-prod` | Same class of machine | Realistic timing, latency figures, long soaks. |
| `arm64-prod` | Raspberry Pi 4B, 2 GB | Second architecture, and a different USB host controller (dwc2, not xhci) — a behavioral difference, not a detail. |
| `arm64-debug` | Raspberry Pi 4B, 2 GB | Lock and memory checking on dwc2. Slow, so reserved for URB-lifetime and reset changes. |
| `armhf-prod` | Raspberry Pi 1B, 512 MB | 32-bit codec path, uniprocessor, no preemption. Sleeping in atomic context is fatal here and benign elsewhere. Slow; kernel switching is painful. Board-specific findings tracked in **[`re/pi1test_platform_notes.md`](../re/pi1test_platform_notes.md)**. |
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
`HOTPLUG` · `INTEROP`

IDs never encode the level or the mode — both change over a case's life, and an
ID that changes is worthless. `JT-MIDI-002` is the LED check whether a human
watches the LEDs or a photodiode ever does.

The mode is not even fixed for a given *run*: `JT-PROBE-003` runs automatically
on a machine with a ppps hub and is done by hand on one without, and it is the
same case with the same ID and the same pass criterion either way. What varies
is who does the work — which is exactly what an identifier must not depend on.

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

### Three modes of execution

A case's **mode** says how much of it a machine can do. It is orthogonal to its
level: L3 holds both a scripted channel sweep and a human LED check.

| Mode | What it means |
|---|---|
| `automated` | Runs start to finish with nobody present. Safe for CI and for an overnight profile. |
| `semi-automated` | The machine does the setup, the actions and the bookkeeping; a person contributes the one thing it cannot — a finger on a fader, or a judgment about what came out. |
| `manual` | A person performs it, with no support beyond written steps. |

The line that matters is between the first two, because it decides whether a
run can be left alone. `runner.py` is attended by default; `--unattended`
withholds the `human` capability, so every semi-automated case is recorded as
pending rather than blocking forever on an answer nobody is there to give.
That requirement is **derived from the mode**, not listed per case — a case
that forgot to declare it is precisely the case that would hang a nightly run.

Semi-automated is not a diminished form of automated. `JT-MIDI-001` is the
shape of it: the machine opens the port, prompts for a particular control to be
moved, watches for the event and decides the verdict. What is left for the
person is the physical act, which no amount of code supplies. The setup, the
watching and the recording — most of the labor, and all of the tedium — are
still gone.

### Manual is also a fallback, not only a mode

A case can be *written* as automated and still be *performed* by hand, because
the equipment that automates it is not always attached. `JT-PROBE-003` cycles
the device's power through a ppps-capable USB hub; on a machine without one,
the same test is a person unplugging a cable ten times and watching the card
index. The test does not change — only who does the unplugging.

So capabilities are resolved for the machine at run time, and the runner takes
the best available form:

1. the automated or semi-automated implementation, when the hardware it needs
   is present;
2. otherwise the manual steps, recorded as pending and rendered by
   `checklist.py`;
3. otherwise blocked.

Only the third loses coverage, and it is reserved for cases where a person
genuinely cannot substitute. **The fallback applies to actuators only** — the
hub, the mains relay: equipment that performs a physical action a human can
perform instead. A missing controller or an unplugged loopback cable blocks,
because there is nothing for an operator to do either, and a checklist politely
asking somebody to test hardware that is not connected is worse than an honest
gap.

Manual cases, semi-automated cases and demoted ones all land in the same result
record as the automated ones, so coverage is one picture rather than two.
`checklist.py` renders whatever a run left pending and reads the answers back
into that same record — carrying the reason a demoted case is being done by
hand, so the operator knows the automation exists and did not run rather than
assuming this is simply how it is done.

### Capabilities belong to the machine, not the target

A case declares what it needs from its surroundings in `requires`. Those tokens
used to be listed per target, and that was keyed wrong: a target is a *build*,
and `x86_64-debug` and `x86_64-prod` are the same EliteDesk booted differently,
so a cable plugged into it had to be declared twice and kept in sync by hand.
Worse, the volatile ones — is the loopback cable connected today? — do not
belong in a file that is meant to be static.

They are resolved instead from three sources:

| Source | Examples | Where |
|---|---|---|
| **probed** | device, root, sox, qemu, rtcwake, the ppps hub | asked of the machine; nothing to maintain |
| **declared** | speakers, loopback cable, signal source, mains relay, second unit, quiet machine | `~/.config/jockey3/capabilities.yaml` |
| **invocation** | `human` | whether `--unattended` was passed |

The declarations file sits outside the repository deliberately: the working
tree is a Seafile share, so a file inside it would sync one machine's rig to
every other. An absent file grants nothing, because a fresh machine silently
claiming a loopback cable it does not have would record a meaningless pass.

**A declaration can only ever take a capability away.** Setting `usb-switch:
false` is a useful "the hub is there, but do not power-cycle today"; setting
`loopback-cable: true` does not put a cable in the socket, and for anything
probed the machine's answer wins.

Which capabilities held is recorded in every run. Without that, a pass from a
day the cable was connected reads identically to one taken with it coiled on
the bench, and §11's staleness reporting cannot tell coverage from its absence.

### Privilege

**The runner is an ordinary user process.** Almost everything a case does needs
no privilege at all: opening the card, playback, capture, MIDI, stopping the
session's sound server, reading sysfs and `/proc/config.gz`. `/dev/snd/*` is
`root:audio`, so membership of `audio` is the whole requirement.

A handful of operations do need root — loading and unloading the module,
reading `dmesg` under `dmesg_restrict=1`, writing the boundary marker to
`/dev/kmsg`, the dynamic-debug rule, the console loglevel, the ALSA xrun-debug
flag, `rtcwake`, and switching the device's hub port power. Every one is a verb
in one root-owned script, `tests/hw/priv/jockey3-testctl`, reached through a
single sudoers entry. That script *is* the privilege boundary, and reading it
tells you everything the suite can do as root.

The port-power verb shows the shape the boundary has to keep. It takes an
**action** — `status`, `off`, `on`, `cycle` — and never a target: the hub and
port are resolved inside the script from the Jockey 3's own USB ids, and only a
port on a hub advertising `ppps` is accepted. A verb that accepted a hub
location would grant the suite the right to cut power to any port on any hub,
and on the test rig the hub one level up carries the keyboard, the mouse, and
the hub the device itself hangs from.

The suite used to require `sudo ./runner.py`, which made every case root for
the benefit of a handful of operations and left result files owned by root.
Granting sudo per binary instead would be worse than it looks:
`NOPASSWD: /sbin/modprobe *` is the right to load any module, which is
unrestricted root.

**Installing a module is deliberately excluded.** Copying a `.ko` into
`/lib/modules` and loading it is arbitrary kernel code execution, so wrapping
it in a narrow-looking verb would make the allowlist presentational rather than
a real boundary. Deploying a new build asks for a password, once, in
`actions/reload_driver.sh`; cycling the already-installed module is automated,
which is what an unattended profile actually needs.

`selftest.py` checks the boundary cannot erode: that every verb the Python
calls exists in the script, that a generated marker token passes the script's
own validation, and that nothing outside `lib/priv.py` reaches for privilege.

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
and the card is resolved by driver match. Both are restored afterwards.

Raising `kernel.printk` is *not* yet part of preparation. The verb exists in
the privileged helper — adding one later would mean reinstalling on every test
machine — but nothing calls it. Console verbosity has not so far been the thing
standing between a failure and its explanation; `dmesg` is captured in full
either way, and the console only matters for a hang, which is what the serial
capture is for.

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

### `dev_dbg()` output is classified by priority, not by rule

`dmesg-read` runs `dmesg --raw`, so every line keeps its `<N>` syslog priority.
The driver reports real problems through `dev_warn()`/`dev_err()`; everything at
`KERN_DEBUG` is a trace. So the classifier treats **any line that is ours and
`<7>`** as expected, without a rule per message. Turning dynamic debug up during
an investigation (`dyndbg=+p`, or the targeted `dyndbg-*` verbs) therefore does
not turn a run red, and adding a `dev_dbg()` to the driver needs no matching
`rules.yaml` entry. Defect detection is unaffected — the escalation pass below
runs before the priority check, so a `BUG:`/`WARNING:` inside a debug line still
aborts. The only `dev_dbg()` rules left in `rules.yaml` are the few that also
feed a metric, plus the rate limiter's prefix-less `"N callbacks suppressed"`
summary, which carries no priority.

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
| L5 PCM | xruns per hour, memory growth, and drift in all of the above across an extended run |
| L5 PROBE | bind/unbind duration; leak or failure across thousands of cycles |
| L5 MIDI | URB stall onset/recovery timing across a sustained run |

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
    dmesg.txt       kernel log, trimmed to this run
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

The table above answers "is JT-RATE-001 covered" for someone who already knows
the catalog. It does not answer "how is this project doing" at a glance, which
is what a GitHub visitor wants. `ledger.py --matrix` renders the same index as
a pivot — cases down, targets across, one glyph per cell — and drops the
detail (revision, commit count, age) down to a legend instead of a column, on
purpose: a pivot that also tries to carry that detail stops being readable
from across the room. Freshness collapses to two states instead of a number —
tested against the exact commit HEAD is at now, or not proven current, which
covers both a real commit gap and a machine with no checkout to measure one
from. The published copy lives at [`test_status.md`](test_status.md); like the
coverage table it is republished by hand — run the command, paste the output
over the generated section, commit.

The matrix reflects committed code only. A run against a dirty tree —
bench iterations, work in progress — is dropped from consideration entirely
rather than labelled: a pass on a scratch edit that never got committed has
no place in a status published to the repository, even amber. Dirtiness is
read from the driver-scoped `kernel_driver_dirty` flag the build manifest
carries, the same one `driver_rev()` prefers, so a classification-rule edit
elsewhere in this repository does not disqualify a driver build that was
verbatim the committed source. `ledger.py`'s plain coverage table is
unaffected — it is the working view, and a developer looking at it wants to
see the run they just took, dirty tree or not.

L1 and L2 are source-level checks, not per-target ones, so they only ever
populate the `(source)` column — and only when collected — unless the catalog
marks a case `per_target: true`, an escape hatch for the rare L2 case whose
*result* is a property of the host rather than the source (JT-CODEC-005's
throughput numbers, not a pass/fail verdict). `checkpatch`,
`build_jockey3.sh` and the KUnit/bench runners produce no `run.json` on their
own; running them directly, as the day-to-day build commands earlier in this
document do, leaves that column blank forever. The `build` profile in
`profiles.yaml` exists to close that gap: `./runner.py --profile build` on
the build host runs the same gates through `cases/build_gate.py`,
`cases/codec_kunit.py` and `cases/codec_bench.py` and records the result.
Because a build-only run never loads the module, its driver identity cannot
come from the usual "what is loaded" check — `env.built_driver_info()`
reads the git revision back from the manifest the build step just wrote for
the `.ko` on disk, so these rows get a real freshness verdict instead of
sitting permanently amber for having no loaded module to point at.

**Freshness is scoped to the driver's own source files, not the whole
repository, and this is load-bearing rather than cosmetic.** Publishing
`test_status.md` is itself a commit, and whole-repository commit distance
would count that commit against every result the matrix had just reported as
current — the act of publishing a fresh matrix would immediately relabel it
stale on the next run, before a single line of driver source had changed.
`commits_since()` restricts `git rev-list --count` to the file list
`sync-driver.sh --list` already maintains as the single point of truth for
what a build actually compiles, so a commit that only touches docs, RE notes,
the test framework, or this ledger's own published output does not move the
counter. Whole-repository distance was tried first and rejected for exactly
this reason.

---

## 11a. When the instrumentation runs out: wire-level tracing

There is a hard boundary to everything above. `/proc/asound` can observe the
driver up to the point where it hands bytes to usbcore, and no further. URB
scheduling, host-controller behavior, the timing of packets on the wire and
the device's own FIFO are all past it.

That boundary is not theoretical here, because this driver's URBs **run free**
for the device's lifetime — playback must keep flowing since it carries MIDI
OUT. So a late resubmission starves the *device* rather than underrunning the
*ALSA buffer*, and produces a defect that is audible and completely unreported:
`xrun_counter` stays at zero, `avail_max` does not move, and no message
reaches the log. §4 records a measured instance of exactly this on
`x86_64-debug`.

**The escalation path is an OpenVizsla trace of the playback endpoint.** The
tooling and the recipe are in
[`re/usb/openvizsla/README.md`](../re/usb/openvizsla/README.md); this section
is about when to reach for it and what it can settle.

### When it is warranted

Reach for a trace when **all** of these hold:

- an audible or measured defect is reproducible;
- `xrun_counter` is zero and `avail_max` is unremarkable, so ALSA supplied
  everything on time;
- removing userspace from the path (play from a file, not a `sox |` pipe)
  does not change it;
- and it happens on a **production** kernel.

The last condition is what makes it worth the effort. On a debug kernel the
explanation is already known and already actionable — KASAN inflates the
completion handlers, quality verdicts move to `x86_64-prod`, and a trace would
refine the mechanism without changing any decision. On a production kernel the
same symptom means the driver is not keeping the ring fed under conditions
users will meet, and the wire is the only place that answer exists.

### What it can and cannot settle

| Question | Trace answers it? |
|---|---|
| Were packets late, and by how much? | Yes — this is what it is for |
| Did the ring drain, or did the host controller stall? | Yes, from the gap pattern |
| Was the *data* correct? | No — loopback (JT-AUDIO-002) checks presence and channel map, not bit-exact content; nothing does yet |
| Is the codec packing right? | No — that is the KUnit suite and the bench |

Timing and integrity are separate questions with separate instruments, and a
trace answers only the first. Reaching for one to answer the second is how an
afternoon disappears.

### Why this is unusually available here

Most driver work cannot do this at all. This project has an OpenVizsla, a
parser that already reduces raw captures to transactions
(`re/usb/parse_openvizsla.py`), and reference traces of the macOS and Windows
drivers to compare against. The protocol was reverse-engineered this way in the
first place, so the capability is proven rather than aspirational.

That is a reason to keep it in mind, not a reason to use it. It is the last
step, not the first.

---

## 12. Roadmap

**Now:** the catalog, the runner, and a small set of automated cases proving
the plumbing end to end.

### Open findings awaiting a decision

These are not roadmap items. They are things the suite has already surfaced and
nobody has resolved, recorded here so they are not quietly forgotten.

**`PM: parent 1-13:1.0 should not be sleeping`**, seen during `JT-PM-001`,
deliberately left unclassified pending a decision: fix, or allowlist with a
reason. See **[`re/pm_suspend_warning.md`](../re/pm_suspend_warning.md)**.

**Mid-stream URB stall after hours of clean operation** — an 8h duplex soak
that stalled on both rings after 4.1h of otherwise clean operation, alongside
a process-gap lesson about capturing device state before power-cycling a
wedged unit. See the 2026-08-23 entry in
**[`re/playback_stall_wedge.md`](../re/playback_stall_wedge.md)**.

**Nothing yet proves data integrity.** Every automated case measures timing or
liveness. `xrun_counter`, `avail_max`, byte counts and stall counts all say
whether audio flowed on time; none says whether the bits arriving were the bits
sent. `JT-AUDIO-002` (implemented 2026-08-19) does not close this either — the
round trip it uses is analog (DAC, cable, ADC), which loses bit-exactness
regardless of what pattern is played, so this remains open. What it does prove
is output presence and channel map, gain-invariantly: every verdict is a ratio
measured within one capture (a tone against the other known tones, a tone
against a floor bin), so a bench's input gain, which the case has no way to
read, cannot flip the result the way a fixed RMS threshold would have. A
tone-per-channel plus a Goertzel bin per tone was used instead of a ramp and
correlation, because correlation needs a lag search between `aplay` and
`arecord` — two processes with no shared clock — and a frequency read does
not.

**Next, in rough priority order:**

1. ~~**Audio loopback**~~ — done, as `JT-AUDIO-002`/`cases/audio_loopback.py`.
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
