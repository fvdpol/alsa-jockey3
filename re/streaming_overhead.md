# Study: reducing the host overhead of continuous URB streaming

Options for cutting the CPU, interrupt and power cost the Jockey 3 imposes on
its host: transfer coalescing, idle rate downshift, and on-demand streaming.

**Status: E1 (baseline measurement) and E2a (firmware acceptance gate) done,
both on real hardware. E2a passed at N=2 in both directions -- see Part 2.**
The companion document `re/streaming_overhead_experiments.md` is the
executable plan for the experiments this study calls for and tracks
per-experiment status in more detail.

This started as a narrower question -- "should the driver stop streaming when
the device is idle?" -- and the answer reframes it, which is why the title is
broader than the question: the load that prompted it is dominated by a
*sizing* choice this driver has never tuned, not by the continuous-streaming
model itself. Stopping the stream turns out to be the least attractive of the
options, and the one nobody had proposed is the largest.

Motivation: `pi1test` (Raspberry Pi 1B) was observed at 30,000-33,000
interrupts/s and 50-60% system time with the Jockey 3 streaming at 88200 Hz,
to the point where new ssh connections could not complete their banner
exchange (`re/pi1test_platform_notes.md`). The device imposes a constant
background cost on the host for as long as it is plugged in, whether or not
anything is using it.

## Summary of the recommendation

Four independent levers exist. They are not alternatives to one another;
levers 1, 2 and 4 compose freely.

| # | Lever | Idle cost at 96 kHz | Streaming cost at 96 kHz | Deviates from vendor model? | Risk |
|---|---|---|---|---|---|
| 1 | Status quo: continuous, 1 packet per URB | 21,600 completions/s | 21,600/s | no | none |
| 2 | Idle downshift to 44.1 kHz | 9,923/s | unchanged | no | low |
| 3 | On-demand start/stop | 0/s | unchanged | **yes** | high, unquantified |
| 4 | Transfer coalescing (N packets per URB) | 2,400/s | 2,400/s | no (wire-identical) | moderate, mechanical |
| 2+4 | Both | **1,102/s** | 2,400/s | no | as lever 4 |

**Recommendation: pursue lever 4 first, then lever 2, and do not pursue
lever 3.**

Lever 4 removes roughly 90% of the per-packet host overhead *while streaming*,
which is where the load actually hurts, without deviating from the
continuous-stream model that the vendor traces validate and that MIDI OUT
depends on. It has direct precedent in an independent Ploytec driver. Lever 2
is Frank's original idea, is cheap, and has already been demonstrated live on
`pi1test`. Lever 3 buys, over and above 2+4, only the difference between
~1,100 completions/s and zero -- in a window (device plugged in with no
software running at all) that is both narrow and the one case where the user
could simply unplug the device -- and it pays for that with a deviation from
the only known-good reference implementation, on firmware that has already
been shown to be fragile about restarting.

---

## Part 1: what the load actually is

### The model

One URB carries exactly one 512-byte Ploytec packet today
(`jockey3_init_playback_urbs()`, `jockey3_init_capture_urbs()`,
`jockey3.c:2561` and `:2595`), with a ring depth of `JOCKEY3_N_URBS` = 8 per
direction. A playback packet carries `PLOYTEC_PLAYBACK_FRAMES` = 10 PCM
frames; a capture packet carries `PLOYTEC_CAPTURE_FRAMES` = 8. So the URB
completion rate is fixed by the sample rate:

| Rate | Playback pkt/s | Capture pkt/s | Total completions/s |
|---|---|---|---|
| 44100 | 4,410 | 5,512 | **9,923** |
| 48000 | 4,800 | 6,000 | 10,800 |
| 88200 | 8,820 | 11,025 | 19,845 |
| 96000 | 9,600 | 12,000 | 21,600 |

Plus one MIDI IN URB on EP 0x83, whose completion rate is data-driven and
currently unmeasured.

### Checked against E1's own measurements (`JT-PERF-001`)

Superseding the `vmstat`-based table this section originally carried (kept
below for the record): E1's tooling produces a clean per-rate measurement
directly, and a full sweep ran on `x86_64-prod`, `arm64-prod` and
`armhf-prod` (`pi1test`) on 2026-08-25/26.

**`x86_64-prod` and `arm64-prod` confirm the flat per-URB model closely at
every rate**, with no SOF-latch term needed (neither board has one): observed
`irq_per_s` at idle and each streaming rate lands within a few percent of the
`playback_pkt/s + capture_pkt/s` table above (e.g. `x86_64-prod`
`stream_88200` measured 19,850/s against 19,845 theoretical; `stream_96000`
21,569/s against 21,600). `cpu_pct_sys_irq_soft` scales smoothly and
sub-linearly with rate on both -- streaming never approaches saturation on
either board (`arm64-prod` peaked at 1.21% at 96 kHz; `x86_64-prod` at 3.67%,
oddly higher despite typically being the faster core -- not yet explained,
worth a look before trusting cross-platform CPU% comparisons).

**`armhf-prod` (`pi1test`) confirms the model at low rates and breaks it
badly at high ones:**

| point | driver traffic | model (driver + 8,000 SOF) | observed `irq_per_s` | ratio |
|---|---|---|---|---|
| unbound | none (driver detached) | 8,000 | 8,014 | 1.00x |
| idle | device operational, no audio stream open, URBs running at 44100 (last-set rate) | 17,923 | 16,591 | 0.93x |
| stream 44100 | audio open at 44100 | 17,923 | 16,530 | 0.92x |
| stream 48000 | audio open at 48000 | 18,800 | 17,176 | 0.91x |
| stream 88200 | audio open at 88200 | 27,845 | **54,995** | **2.0x** |
| stream 96000 | audio open at 96000 | 29,600 | **92,565** | **3.1x** |

`unbound` is the true zero: no URBs at all, and it lands on the SOF-latch
figure alone almost exactly -- the cleanest confirmation available that the
8,000/s baseline is a property of the board, not of this driver. `idle` (URBs
running, nothing open) and `stream_44100` (a substream open, at the same
44100 Hz the device already happened to be at) are conceptually different
states -- one is the device's resting condition, the other is actively
carrying audio -- but they share the same completion rate, since URBs run
free regardless of PCM open/close, and both land close to the model.

44.1 and 48 kHz land within 10% of the additive model (driver completions
plus the board's own 8,000/s dwc2 SOF-latch baseline), matching the earlier
`vmstat` reading almost exactly (16,591-17,176 here against 16,700-16,900
before) and confirming the model is right at rates the board can keep up
with. At 88.2 and 96 kHz the observed rate is two to three times the flat
model, alongside `cpu_pct_sys_irq_soft` of 97% and 95% -- a board at those
rates is not just busier, it is saturated, and "one interrupt per URB
completion" stops holding. The most likely mechanism, not yet confirmed
against dmesg from the same run: a feedback loop where a CPU too slow to
service URB completions on schedule trips the watchdog's stall detection
(`JOCKEY3_WATCHDOG_STALL_MS`, `jockey3.c`), which restarts the URB ring or
escalates to a full reset -- both of which are themselves more interrupt
traffic, on a CPU that was already the bottleneck. If confirmed, the excess
at high rates is a symptom of recovery activity compounding the load it was
triggered by, not a second, larger baseline term the flat model is simply
missing.

This does not change the study's recommendation or its scope: `armhf-prod`'s
bar is "does not crash, hang, or oops" (`docs/test_strategy.md`), not
"stays fast", and none of the four levers here is being pursued to fix pi1
specifically. But it is worth folding into any future revision of the
completion-rate model: flat and multiplicative below some saturation
threshold, non-linear and recovery-driven above it, and that threshold is a
property of the host, not of the driver.

(`x86_64-prod` and `arm64-prod` both measured `irq_per_s_unbound` as 0 --
neither has a SOF latch, so their true zero really is zero.)

**And a second, live data point for the recovery-feedback hypothesis, from
the same run.** `JT-PERF-001` itself tries to leave the device at 44.1 kHz
when it finishes (`restore_resting_rate()`, added after the first pi1test
run); on this run that attempt itself timed out immediately after the
96 kHz point, with the board still at ~95% CPU. The case gave up on it after
10s (now reported as a case failure, not a buried note) and exited -- but the
runner's own next step then hung for several more minutes, clearing
**immediately** the moment the device was physically unplugged. Since
unplugging removes only the device's own traffic and not the board's
independent 8,000/s SOF baseline, a hang that clears that fast points at
something the *device* was still doing after the case's own process had
already exited -- consistent with an in-flight recovery/reset ladder
(`jockey3_recover_urb_stream()`, `usb_queue_reset_device()`) still running in
kernel context, unaffected by the userspace process that triggered it having
been killed. Not confirmed against dmesg (pi1test's small `log_buf_len`
lost it, same gap noted in the platform notes), but if right, it means a
rate change issued immediately after a saturating high-rate stream is not
reliably clean on this board -- a concrete, load-bearing data point for
however lever 2 (idle rate downshift) ends up gating itself in Part 3, not
just a test-tooling wrinkle.

<details>
<summary>Superseded: the original single `vmstat` reading (kept for the record)</summary>

`pi1test`, `vmstat 1`, from `re/pi1test_platform_notes.md`, before E1's
tooling existed:

| State | Model (driver + 8,000 SOF) | Observed | Residual |
|---|---|---|---|
| 44100 Hz streaming | 17,923/s | 16,700-16,900/s | -1,000 to -1,200 (~7%) |
| 88200 Hz streaming | 27,845/s | 30,000-33,000/s | *not a clean baseline* |

The 88.2 kHz reading was taken **during the `JT-AUDIO-002` wedge** -- `arecord`
stuck in D state, five playback watchdog stalls, bursts of `Failed to resubmit
playback/capture URB: -19`, and recovery cycles running -- so it measured a
fault, not a baseline, which is exactly why E1 was built rather than trusting
hand-run `vmstat` further. The 44.1 kHz reading holds up against E1's own
measurement at the same rate.

</details>

### What "interrupt rate" does and does not tell you

**On modern x86 hosts, IRQ count is not URB count.** xHCI implements
interrupt moderation and event coalescing, so a host that completes 21,600
URBs/s may raise far fewer interrupts. IRQ/s therefore *understates* the work
on exactly the platforms where it is easiest to measure. The portable metric
is CPU time, not interrupt count. See Part 5.

### Where the time goes, and what each lever can reach

Per completion the driver does: the host controller's own IRQ entry and event
processing, a softirq dispatch, `jockey3_playback_callback()` /
`jockey3_capture_callback()` (URB status check, one atomic decrement, one
`ktime_get_mono_fast_ns()`, a spinlock round trip, the codec conversion of
10 or 8 frames, and a resubmit through `usb_submit_urb()`). On top of that,
whenever a PCM substream is actually open, `aplay`/`arecord` are issuing a
steady stream of `read()`/`write()` syscalls to move data through the ALSA
ring buffer -- kernel-context time on every call, invisible to `irq_per_s`.

Only the codec conversion and the read/write syscall traffic scale with
*bytes processed*. Everything else -- IRQ entry, softirq dispatch, the
completion handler's own fixed-cost bookkeeping, resubmission -- scales with
*URB count* alone. This is the crux, and it decides what coalescing (lever 4)
can and cannot reach:

- Lever 4 removes the per-URB fixed cost and leaves both the codec work and
  the syscall traffic unchanged -- fewer, larger transfers move the same
  bytes through the same encode/decode path and the same `read()`/`write()`
  calls.
- Levers 2 and 3 reduce the byte rate as well, but only while idle.

**Measured, not just reasoned about: the per-frame component is not small.**
The first pi1test run caught `idle` and `stream_44100` at the *same*
completion rate (URBs run free regardless of PCM open/close, so opening a
stream at whatever rate is already running changes nothing about
`irq_per_s`) -- 16,591 vs 16,530/s, statistically identical.
`cpu_pct_sys_irq_soft` was not: 12.09% idle, 26.46% streaming. That entire
14.4-point gap is the per-frame component at a rate lever 4 does not touch.
This does not overturn the recommendation, but it means coalescing's payoff
should not be assumed proportional to its ~89% completion-rate reduction.

**Follow-up, 2026-08-25/26: `JT-PERF-001` redesigned to measure `idle_R` and
`stream_R` at every rate, not once.** A single unqualified `idle` point,
measured at whatever rate happened to be left over, could only make the
idle-vs-streaming comparison incidentally. The case now sets each rate
explicitly, samples idle there, then immediately opens a stream at the same
rate -- giving the per-frame component at every rate, on every platform, in
one run. Full results in `re/streaming_overhead_experiments.md`; the
headline findings:

- **On `x86_64-prod`, the per-frame `cpu_pct_sys_irq_soft` gap is real at
  every rate but small and shrinking** -- roughly +1.0 point at 44.1 kHz,
  down to +0.08 at 96 kHz, against idle baselines themselves only 0.1-2.3%.
  At these magnitudes a single un-repeated 10 s sample is close to the noise
  floor; treat the shrinking trend as suggestive, not established, until
  E1 gets a repeated-sampling option.
- **`ns_per_playback_cb`/`ns_per_capture_cb` are not comparable across
  platforms.** `x86_64-prod` measured 1,400-1,650 ns/call; `arm64-prod`
  measured 6,000-9,700 ns/call at the exact same rates -- a 4-7x gap that
  contradicts `arm64-prod`'s *lower* `cpu_pct_sys_irq_soft` at the same
  completion rate (the open question from the first sweep, still open). The
  likely explanation is that `ns_per_*_cb` is measured through
  `function_graph` tracing (E1's `trace-callbacks` verb), and ftrace's own
  per-call instrumentation overhead is itself architecture-dependent and can
  differ substantially between x86_64 and arm64. Until measured independently
  of tracing, `ns_per_*_cb` should be read as *within one platform, one run*
  only -- `cpu_pct_sys_irq_soft`, taken with tracing off, remains the
  portable number.
- **`armhf-prod`'s same-day re-run is not usable as clean data.** CPU was
  already at 96-99% by the *first* `idle_44100` point, before any streaming
  had happened, and `stream_44100`'s CPU (42.84%) then read *lower* than the
  `idle_44100` that preceded it -- the wrong direction for a real
  idle-vs-streaming comparison, and a strong sign the board was still working
  through leftover load from something else (see the recovery-storm
  hypothesis above) rather than measuring a clean resting state.
  `idle_96000` failed outright (`could not set 96000 Hz`). Given this, and
  the confounds already known for `armhf-prod` at high rates, no
  per-frame/per-URB split should be drawn from this run's pi1test numbers.
- **The post-PASS runner hang reproduced.** On this same `armhf-prod` run,
  once results were being written the runner hung again, and the operator
  again unpowered the device rather than wait it out -- the second such
  occurrence in two days, in the same place (after the sweep, before the
  runner finishes its own post-case work). Two for two is enough to call this
  a reproducible symptom of this board under this case's load pattern, not a
  one-off; still not confirmed against dmesg.

---

## Part 2: lever 4 -- transfer coalescing

### The device paces the wire, so coalescing is invisible to it

This was checked against the raw OpenVizsla traces rather than assumed.

Inter-packet gaps on the macOS captures, measured on the parsed transaction
logs (`usb capture 2026/capture_macos_96k_512_parsed.txt`,
`capture_macos_44k_poweron_parsed.txt`):

| Capture | EP 0x05 OUT modal gap | EP 0x86 IN modal gap |
|---|---|---|
| macOS 96 kHz | 90-110 us (theory: 104.2 us) | 70-90 us (theory: 83.3 us) |
| macOS 44.1 kHz | 210-240 us (theory: 226.8 us) | 170-190 us (theory: 181.4 us) |

Packets arrive **one per packet interval, evenly spaced -- never in
back-to-back bursts**, even though the host has multiple transfers queued.
The raw trace shows why: the wire between data packets is full of
`PING`/`NAK` and `NYET` handshakes (63,353 NAKs in the first 200,000 lines of
`capture_macos_96k_512.txt`). The device refuses data until its FIFO is ready
and paces the stream itself; the parser discards those handshakes, which is
why the parsed log looks like a clean metronome.

Two consequences:

1. **The corpus cannot distinguish a 512-byte transfer from an N x 512-byte
   transfer, and in principle never could.** A bulk transfer larger than
   `wMaxPacketSize` is delivered as full-size 512-byte packets with no wire
   marker for the transfer boundary. The existing note
   (`re/usb/openvizsla/buffer size tests.md`) that the wire "is still using
   the 512 byte Ploytec framing" regardless of the CoreAudio buffer size is
   correct and remains correct -- it just does not answer this question.
   Confirmed again independently: `re/usb/openvizsla/capture_2026-08-17_macos_ratechange.md`
   documents a real 50.3-51.4 ms "quiet window" in vendor traces, initially
   worth checking as a possible coalescing tell -- it turned out to be
   pacing between register writes inside the EP0 rate-change control-transfer
   handshake, unrelated to the PCM bulk endpoints, and already spent on
   fixing Milestone 13's original capture-stall bug (`re/rate_change_stall.md`).
   No gap, burst pattern or timing signature on the wire distinguishes
   coalesced from uncoalesced bulk transfers, in any capture examined so far.
2. **It does not need to.** Because the device paces, an N-packet URB simply
   takes N packet intervals to retire and produces one completion instead of
   N. The wire traffic is byte-for-byte identical. The device cannot tell the
   difference.

### Independent precedent: the Ozzy driver already does this

`mischa85-Ozzy-Linux` targets other Ploytec devices (Xone DB4/DB2/DX/4D,
Wizard 4) with the same 512-byte sub-packet framing, the same 10-frames-out /
8-frames-in geometry, and the same EP numbering
(`common/devices/ploytec/ploytec_defs.h`):

```
PLOYTEC_BULK_OUT_SUBPKT_SIZE  512    /* per sub-packet */
PLOYTEC_FRAMES_PER_OUT_SUBPKT 10
PLOYTEC_FRAMES_PER_IN_SUBPKT  8
PLOYTEC_FRAMES_PER_PKT        80     /* audio frames per USB transfer */
PLOYTEC_BULK_OUT_PKT_SIZE     4096   /* 8 sub-packets (512 * 8)  */
PLOYTEC_IN_PKT_SIZE           5120   /* 10 sub-packets (512 * 10) */
OZZY_PCM_N_URBS               4
```

It submits **8 packets per playback URB and 10 per capture URB** -- 80 PCM
frames each way, so both directions have equal-duration transfers -- with a
ring depth of 4. That is an independent implementation, against related
firmware, choosing 8-10x coalescing. (Noted as evidence only; no code is
copied from that tree, per project policy.)

**Be precise about what this establishes.** Ozzy targets Xone/Wizard devices
at vendor `0x0A4A` with 8 channels; the Jockey 3 is Reloop `0x200c` at 4
playback / 6 capture channels. The precedent shows that the Ploytec 512-byte
sub-packet framing *as a design* tolerates multi-packet bulk transfers -- it
does **not** establish that this particular firmware accepts a 4096-byte bulk
OUT. That is the real open risk, and it is the first thing E2 tests.

### E2a result: gate passed at N=2, on real Jockey 3 hardware

Tested on `alsa-test`, `dev/streaming-overhead` branch, with a build-time
probe (`JOCKEY3_E2A_COALESCE_PROBE`, `jockey3.c`) that widens both a single
playback and a single capture URB from 512 to 1024 bytes (N=2), leaving
everything else -- ring depth, callback structure, period accounting --
untouched. Sub-packet 0 still carries real audio via the existing path;
sub-packet 1 is filled with idle/sync framing only, since the real
per-sub-packet loop is E2b's job, not this probe's.

- **Capture: clean.** `actual_length` reported 1024 (the full requested
  transfer, not a short one) for 35+ seconds continuously on the first run,
  and again from device init on a second run, with zero errors. The device
  does not truncate a multi-sub-packet capture URB to one sub-packet.
- **Playback: clean on the decisive run.** The first hardware run showed one
  isolated self-recovered stall (`restarted after stalling for 80 ms`) early
  on; a second, longer run completed a full playback with **zero kernel
  messages** -- no stalls, no URB errors. The one stall did not reproduce and
  is treated as noise, not a firmware limitation.
- **Audible distortion during the probe (ring-modulated / half-speed sound)
  is expected and is not a correctness signal.** It is a direct consequence
  of sub-packet 1 carrying silence instead of real audio in this
  acceptance-only probe, not evidence of a wire or firmware problem. It
  should disappear once E2b fills every sub-packet with real data.
- Both runs' termination via an EPROTO storm + "USB disconnect" was traced to
  a deliberate manual power-off of the device between runs, not a firmware
  fault -- see the standing lesson in `.claude/session-state.md` about not
  reading that dmesg signature as a protocol failure without first ruling out
  a physical power event.

**Conclusion: N=2 multi-packet bulk transfers are protocol-viable on real
Jockey 3 firmware, in both directions.** This clears E2a's gate; E2b (the
real sub-packet processing loop, filling every sub-packet with live data) is
next. Higher N (4x, 8x) has not yet been probed and should not be assumed
clean on this evidence alone.

### E2b result: a pre-existing rate-change stall, not a coalescing bug

E2b (real per-sub-packet coalescing, N=2 both directions) sounded clean on
first listen -- the E2a-era distortion was gone -- and `JT-AUDIO-002`,
`JT-MIDI-002`, MIDI OUT responsiveness, `JT-PERF-001` and `arecord` all
passed clean on `alsa-test`. But `JT-AUDIO-002` (which sweeps all four
sample rates) followed immediately by `JT-MIDI-001` reproduced a playback
URB stream stall 2 out of 4 times: the free-running idle playback stream
(no PCM open) stops completing URBs for 400-500 ms, the watchdog's first
restart attempt fails too, and it escalates to a full USB reset. One
occurrence hit mid-`JT-AUDIO-002` itself, with capture stalling
simultaneously.

**This reproduces identically at N=1** -- an N=1 build (byte-for-byte the
pre-coalescing code path per E2b's own regression requirement) hit the same
stall on the same `JT-AUDIO-002` -> `JT-MIDI-001` sequence. **It is not an
E2b/coalescing bug.** It is the rate-change fragility `implementation_plan.md`
Milestone 13 already tracks (there marked "root-caused and fixed; cleanup in
progress" for capture specifically) -- this evidence shows it also affects
Playback, and that both directions can stall together, neither of which the
existing Milestone 13 writeup covers. Worth reconciling there, separately
from E2.

**Conclusion: E2b is not blocked by this.** The coalescing change itself
(N=2, both directions) is clean; the stall is a pre-existing, orthogonal
reliability issue this testing happened to surface. E2c can proceed.

### E2c preliminary result: `x86_64-prod`, N=1 vs N=2, halved completion rate confirmed

The N=2 `JT-PERF-001` run already collected while testing E2b
(`tests/hw/results/x86_64-prod/20260825T195732Z-smoke`) can be compared
directly against the N=1 baseline runs E1 already collected on the same
board (`.../20260825T170234Z-smoke`, with `.../154430Z` and `.../161559Z` as
consistency checks on `stream` alone -- `idle_R` metrics didn't exist yet
when those two ran). No new hardware pass was needed for this first look.

`irq_per_s` halves almost exactly at every rate, as the flat per-URB model
predicts (N=2's one completion now carries two sub-packets):

| Rate | `irq_per_s` N=1 (stream) | `irq_per_s` N=2 (stream) | ratio |
|---|---|---|---|
| 44100 | 9,924 (avg of 3 runs) | 4,957.1 | 0.500 |
| 48000 | 10,794 (avg of 3 runs) | 5,390.9 | 0.500 |
| 88200 | 19,850 (avg of 3 runs) | 9,925.2 | 0.500 |
| 96000 | 21,577 (avg of 3 runs) | 10,796.5 | 0.500 |

`cpu_pct_sys_irq_soft` tells the more interesting story -- the saving is
real but not flat across rates, and is largest exactly where the study
predicted (Part 1's "the per-frame gap is real" finding for `x86_64-prod`):

| Rate | cpu% N=1 idle | cpu% N=2 idle | cpu% N=1 stream (avg) | cpu% N=2 stream |
|---|---|---|---|---|
| 44100 | 0.12 | 0.17 | 1.04 | 1.07 |
| 48000 | 0.20 | 0.12 | 1.20 | 0.68 |
| 88200 | 2.30 | 0.12 | 3.14 | 1.25 |
| 96000 | 2.27 | 0.17 | 2.73 | 1.07 |

At 44.1/48 kHz the CPU saving is within measurement noise -- both N=1 and
N=2 sit near the measurement floor there, and the codec/copy work
coalescing does not touch already dominates whatever is left. At 88.2/96
kHz the effect is large and unambiguous: idle CPU drops roughly **15-19x**,
not the 2x the completion-rate halving alone would suggest. This is the
first direct evidence for the mechanism Part 1 flagged but could not
explain (`x86_64-prod`'s oddly elevated 88.2/96 kHz CPU%, "not yet
explained, worth a look before trusting cross-platform CPU% comparisons") --
consistent with per-completion fixed cost (interrupt entry, softirq
dispatch, URB resubmission) dominating at these rates on this board, which
is exactly the cost coalescing removes and codec work is not.

**Not yet done, before calling E2c complete:** this is one board, one run
each side, `stream` only lightly cross-checked (3 N=1 runs, 1 N=2 run) and
`idle` only single-run each side. But directionally, at the two rates where
it was ever going to matter, the payoff is real and roughly an order of
magnitude larger than the naive completion-rate model alone would have
predicted.

### E2c preliminary result: `arm64-prod`, N=1 vs N=2 -- rate halves, but the x86_64 non-linearity does not appear

Same comparison, `arm64-prod` N=2 run
(`tests/hw/results/arm64-prod/20260825T211430Z-functional`) against the N=1
baseline (`.../20260825T170721Z-functional`, with `154735Z`/`161340Z` as
`stream`-only consistency checks -- same `idle_R` availability gap as the
`x86_64-prod` runs above).

`irq_per_s` halves just as cleanly as on `x86_64-prod` (0.499-0.501 at every
rate) -- the completion-rate model is platform-independent, as expected.

`cpu_pct_sys_irq_soft`, by contrast, does **not** show `x86_64-prod`'s
dramatic non-linearity:

| Rate | cpu% N=1 idle | cpu% N=2 idle | cpu% N=1 stream | cpu% N=2 stream |
|---|---|---|---|---|
| 44100 | 0.65 | 0.32 | 0.72 (avg 0.49-0.84) | 0.67 |
| 48000 | 0.50 | 0.25 | 0.84 (avg 0.79-0.96) | 0.79 |
| 88200 | 0.65 | 0.45 | 1.73 (avg 0.87-1.73) | 1.31 |
| 96000 | 0.97 | 0.30 | 1.73 (avg 1.21-2.0) | 1.01 |

The saving on `arm64-prod` is real but modest -- roughly 1.4-3.2x at idle,
close to what the completion-rate halving alone predicts, and `stream`
barely moves at 44.1/48 kHz. **The 15-19x idle-CPU collapse seen on
`x86_64-prod` at 88.2/96 kHz is specific to that board, not a general
property of coalescing.** This fits Part 1's standing observation that
`x86_64-prod`'s per-frame CPU cost was oddly elevated relative to `arm64-prod`
in the first place (`arm64-prod` peaked at 1.21% at 96 kHz against
`x86_64-prod`'s 3.67% in the N=1 baseline) -- whatever fixed per-completion
cost `x86_64-prod` was paying, `arm64-prod` was not paying nearly as much of
it to begin with, so coalescing has correspondingly less to remove there.
The payoff from lever 4 should be expected to vary by host, not assumed
uniform.

### What it costs

Completion rate with 80 frames per URB in both directions:

| Rate | URB span | Completions/s (both directions) | vs today |
|---|---|---|---|
| 44100 | 1.814 ms | 1,102 | -89% |
| 48000 | 1.667 ms | 1,200 | -89% |
| 88200 | 0.907 ms | 2,205 | -89% |
| 96000 | 0.833 ms | 2,400 | -89% |

Latency and granularity costs, all of them consequences of the URB span:

- **Playback data staleness.** A playback URB's payload is written in the
  completion handler of the previous URB, so PCM data is committed one URB
  span earlier than today. At 44.1 kHz that moves from 227 us to 1.81 ms.
- **MIDI OUT granularity.** MIDI OUT rides byte 480 of each sub-packet, and
  each sub-packet still gets its own byte, so the *throughput* is unchanged.
  What grows is jitter: a byte handed to the driver just after an URB was
  filled waits up to one URB span. Against the driver's own MIDI OUT rate
  limit of ~2500 bytes/s (one byte per 400 us,
  `jockey3_get_next_midi_out_byte()`, `jockey3.c:887`) and the 320 us a MIDI
  1.0 byte takes on a real DIN wire, 1.8 ms of added jitter is inside the
  noise for LED feedback. It should still be measured, not assumed.
- **Minimum period size.** `period_bytes_min` is currently one packet
  (`jockey3.c:1899`). Period elapse is computed per sub-packet inside
  `jockey3_process_out_packet()`, but it can only be *reported* once per URB,
  so the honest minimum period becomes one URB span. This must be raised in
  the same change, or userspace can negotiate a period the driver cannot
  deliver.
- **Ring depth interacts.** 8 URBs x 8 packets is 64 packets in flight --
  14.5 ms at 44.1 kHz, against 1.81 ms today. That is probably too deep;
  Ozzy's 4 URBs (7.3 ms) is a more reasonable starting point, and the depth
  should be re-chosen rather than inherited.

### What it costs to implement

Smaller than it looks, because the per-packet processors are already
separable. `jockey3_process_out_packet()` and `jockey3_process_in_packet()`
(`jockey3.c:512`, `:566`) each take a single 512-byte buffer and return
whether a period elapsed. The completion handlers would loop over sub-packets:

```c
for (i = 0; i < n_subpkts; i++)
        period_elapsed |= jockey3_process_out_packet(chip, buf + i * PLOYTEC_PKT_SIZE);
```

Touch points: URB allocation and `usb_fill_bulk_urb()` lengths; the sub-packet
loop in both callbacks; `jockey3_init_out_packet()` and
`jockey3_silence_out_packet()` per sub-packet; `urb->actual_length` handling
on capture (a short IN transfer must be processed as
`actual_length / PLOYTEC_PKT_SIZE` sub-packets, not blindly as N);
`period_bytes_min`; the packet-interval arithmetic in the
`JOCKEY3_WATCHDOG_*` comment block; and the buffer-size validation in
`buffer_limits_validation.md` (a workspace-root file, outside the repo). The
KUnit codec tests are unaffected -- the codec still sees one packet at a time.

**The constant that actually breaks is `jockey3_check_urb_stream_alive()`'s
liveness window** (`jockey3.c:1540`), hardcoded to `NSEC_PER_MSEC` -- 1 ms --
and documented on the invariant *"a healthy direction confirms itself within
one packet interval -- under 230 us at any supported rate"*. At N=8 and
44.1 kHz the URB span is 1.81 ms, so that sentence becomes false, and a single
1 ms sample of a perfectly healthy stream can legitimately see zero
completions. It is sampled repeatedly inside the 50 ms
`JOCKEY3_PREPARE_CONFIRM_MS` deadline so it would probably still converge, but
the invariant the code is documented on no longer holds, and the
`hw_params()` / `prepare()` liveness checks are precisely where a confusing
false stall would surface during E2. **The rule is: the liveness window must
be at least one URB span.** That constant and its comment change with N.
(`JOCKEY3_WATCHDOG_STALL_MS` at 20 ms is still 11 URB spans in the worst case
and needs no change, but its justification text does.)

### Related, smaller lever: `URB_NO_INTERRUPT`

Setting `URB_NO_INTERRUPT` on all but the last URB of a queued run asks the
host controller not to raise an interrupt for the intermediate ones. It cuts
IRQ entries but **not** the per-URB softirq and completion-handler work, so it
helps a board with expensive interrupt entry (pi1) far more than x86. Whether
xHCI and dwc2 both honor it in practice needs checking before it is counted
on. Strictly secondary to coalescing.

---

## Part 3: lever 2 -- idle rate downshift

Frank's original proposal: after both PCM directions have been closed for a
few seconds, reprogram the device back to 44100 Hz.

**This one already has a live demonstration.** On `pi1test`, with the board
effectively unreachable at 88200 Hz, running a one-second `aplay` at 44100 Hz
to force `jockey3_set_rate(44100)` dropped interrupts from ~30,000/s to
~16,700-16,900/s, system time from 50-60% to 10-20%, and idle from 30-45% to
84-91%. The board became reachable again immediately
(`re/pi1test_platform_notes.md`). That is the whole lever, performed by hand.

**Arguments for:**

- The saving is the ratio of rates: 96000 -> 44100 removes 54% of the
  completion rate. Composed with lever 4 it takes idle cost to ~1,100/s.
- The rate-change path is no longer the liability it was. Since the
  `ploytec_start_streaming()` fix (`5505b28`), it measures 0 stalls in 486
  rate changes, and endurance-validated at 0 resets over 20,000 changes on
  `x86_64-prod` and 4,000 on `arm64-prod` (`re/rate_change_stall.md`).
- A rate change with no stream open is the *safe* case. The existing
  idle-capture gate exists precisely because a reset during a rate change is
  audible on a working stream; with nothing open there is nothing to glitch.
- 44100 Hz is the device's own power-on default and what every vendor cold
  init programs regardless of the application's configured rate
  (`re/usb/init_timing_comparison.md`, Finding 3). Returning there when idle
  is closer to the vendor's resting state, not further from it.

**Arguments against, and the open questions:**

- A rate change is destructive and takes 100-210 ms. The next open at a
  non-44.1 kHz rate pays that cost, where today it might have been free
  because the device was already at the right rate. This is a first-open
  latency regression, bounded and probably invisible, but real.
- A small residual stall rate persists on arm64 (12 playback / 6 capture
  stalls in 4,000 changes, all recovered without a device reset). A downshift
  timer *adds* rate changes to a system's lifetime, so it adds occasions for
  that residual to fire. Against 20,000 clean changes on x86_64 this is minor,
  but it is not zero, and the whole point of the change is to help the
  weakest platform.
- The timer must not fire during an in-progress rate change or reset, and must
  be cancelled by any open. It needs `rate_mutex`, so it must run from a work
  item, not a timer callback.
- **Open question:** does the downshift need to be gated on MIDI OUT activity
  as well? A rate change stops and restarts the URBs, which interrupts the
  MIDI OUT byte stream for the duration. If a controller application is
  driving LEDs with no audio open, a downshift would produce a ~150 ms MIDI
  OUT gap out of nowhere. Probably harmless (MIDI OUT here is LED feedback,
  and the rate-change gap already happens on every hw_params), but it should
  be a deliberate decision rather than an accident.

---

## Part 4: lever 3 -- on-demand streaming

The proposal: keep the URBs stopped while nothing is open, start them on PCM
open or on the first outgoing MIDI byte, stop them again after N seconds of
inactivity.

### The gate question

**Can the device resume bulk OUT and IN after a multi-second gap with no EP0
traffic, within 20 ms?**

Nothing in the corpus answers this, and nothing can: neither vendor ever stops
streaming, so there is no trace of a device being restarted from an idle
stream. `capture_2026-08-13_macos_poweron_noapp` establishes the opposite --
macOS enumerates the device, cold-inits it, programs the rate, and then
streams 141,524 packets over 38.4 s on EP 0x05 **with no application open at
all** (`re/usb/init_timing_comparison.md`, Finding 4). The reference
implementation's answer to "what do you do when nothing is using the device"
is "keep streaming".

Everything the driver knows about this firmware's restart behavior is a prior
against the gate question passing cheaply:

- After a rate change the device's capture engine does not restart until it is
  explicitly re-kicked with an unconditional `SET_STATUS` write. Before that
  was understood, capture failed to restart on roughly **one rate change in
  six** (`re/rate_change_stall.md`). The firmware has a latching notion of
  "streaming" that a host must actively re-arm.
- Nothing establishes whether a host that simply stops submitting leaves that
  bit set, clears it, or leaves the device in a state that needs the alt-0
  bounce. If the bounce is needed, on-demand is dead on arrival on Frank's own
  criterion: macOS cold init spans 103-142 ms, the sequence contains ~50 ms
  *fixed sleeps*, and this driver's own `ploytec_initialize_device()` carries a
  3-5 ms `usleep_range()` plus four `usb_set_interface()` round trips
  (`ploytec_proto.c:120`).
- If only `GET_STATUS` + `SET_STATUS` is needed (`ploytec_start_streaming()`,
  two EP0 transfers, tens to hundreds of microseconds on a healthy bus), the
  budget is potentially reachable. This is the only branch in which lever 3 is
  viable at all.

### Costs that apply even if the gate question passes

- **`jockey3_midi_out_trigger()` runs atomic** (`jockey3.c:2386`; rawmidi
  triggers are called with a spinlock held). Starting URBs needs
  `GFP_KERNEL`, 16 `usb_submit_urb()` calls and possibly EP0 traffic, so it
  must be deferred to a work item. That stacks workqueue scheduling latency on
  top of the restart -- and it does so worst on a loaded, weak CPU, which is
  exactly the machine the change is meant to help. On `pi1test` at 50-60%
  system time, a 20 ms budget for "schedule a work item, run an EP0
  round-trip, submit 16 URBs, and get a packet accepted by a device that paces
  at 227 us" is not obviously achievable.
- **The first MIDI OUT byte after idle is late by the whole restart.** So is
  the first audio after a PCM open, though that is hidden inside
  `hw_params`/`prepare` where a delay is already normal.
- **MIDI IN must be proven to survive.** `jockey3_stop_urbs()` kills the MIDI
  IN URB along with everything else (`jockey3.c:1244`). Keeping it submitted
  while the PCM URBs are stopped is easy in the driver, but whether the
  *device* still delivers MIDI IN when its audio stream has stopped is
  unknown. A controller whose knobs and faders go dead whenever audio is
  closed is a functional regression, not a tradeoff -- and this is a DJ
  controller whose primary function is MIDI.
- **The closest existing analog is not encouraging.**
  `jockey3_suspend()` / `jockey3_restore_device()` (`jockey3.c:3040`,
  `:3062`) is already a stop-then-start, and the restore path does not attempt
  a bare URB restart -- it goes through a full `jockey3_set_rate(cold_init =
  true)`. There is also an open PM suspend warning
  (`re/pm_suspend_warning.md`). That is the driver's own accumulated evidence
  that the restart path is not clean.

### The argument that shrinks the payoff to near nothing

The Jockey 3 is a DJ controller with LEDs, and MIDI OUT is multiplexed into
the playback stream. **Any software doing LED feedback keeps the stream
permanently non-idle** -- the idle timer never fires, whatever its value. The
benefit window is therefore not "whenever no audio is playing"; it is
"plugged in with no controlling software running at all". That window is:

- the one lever 2 already covers, at a fraction of the risk;
- the one where the user can simply unplug the controller;
- worth, over and above levers 2+4, the difference between ~1,100
  completions/s and 0.

That is the whole payoff of a significant architectural deviation from the
only known-good reference implementation, on firmware documented to be
fragile about restarting.

### Conclusion on lever 3

**Not recommended.** Not because it cannot work, but because the payoff
remaining after levers 2 and 4 does not justify the risk, and the gate
question cannot be answered from existing evidence -- it requires new hardware
experiments whose only purpose would be to unlock that small remainder.

If it is pursued anyway, the order is fixed: answer the gate question first
(Experiment E3 below), and stop if it fails.

---

## Part 5: how to measure this reliably

Frank asked this explicitly. Concretely:

**1. Host-controller IRQ delta, not `vmstat`'s aggregate.**
Read `/proc/interrupts` for the specific `xhci_hcd` / `dwc2` line, twice,
over a fixed interval. `vmstat`'s `in` column aggregates everything on the
box and cannot separate the device from the pi1 SOF latch.

Take it at three points, always in this order: **device unplugged**,
**plugged and idle**, **streaming at each rate**. The unplugged baseline is
what makes the other two mean anything -- on pi1 it is what subtracts the
8,000/s SOF latch.

**2. Caveat that must accompany every IRQ number.** xHCI interrupt moderation
means IRQ count is not completion count on modern hosts. An IRQ/s figure is
comparable across configurations *on the same host*, and not comparable
between hosts.

**3. CPU time is the portable metric.** `mpstat -P ALL 1`, summing
`%sys + %irq + %soft`, with the same three-point protocol. This is the number
to quote when comparing coalescing factors, because it captures the work
regardless of how the controller batches its interrupts.

**4. Per-callback cost, for attributing the change.** ftrace `funcgraph` or a
bpftrace histogram on `jockey3_playback_callback` / `jockey3_capture_callback`
gives ns/call. Multiplied by the known completions/s it gives a directly
comparable figure across rates and across coalescing factors, and it separates
per-URB overhead (which coalescing removes) from codec work (which it does
not). This is a development-time measurement using standard kernel tracing on
unmodified source -- no driver test hooks.

**5. Power, not only CPU.** `turbostat` (x86) or `powertop` C-state residency,
same three points. A stream with a wakeup every 100-227 us keeps a CPU out of
deep C-states permanently; 1.8 ms URBs let it sleep between them. On a laptop
this is the strongest single argument for both levers 2 and 4, and it is
invisible in an IRQ count. Note that **coalescing does not reduce bus
activity** -- the host controller keeps PING/NAK-ing the endpoint continuously
either way, so the USB link never leaves L0. Only lever 3 stops that, and
that is its one genuinely unique benefit.

**6. `-prod` configs only.** A KASAN/debug kernel makes the device crackle
audibly while every ALSA counter stays clean
(`docs/debug_kernel_audio_quality.md`); performance numbers taken there are
void.

**7. Run it as a test case, not by hand.** A `JT-PERF-00x` case in
`tests/hw/` that samples `/proc/interrupts` and `mpstat` around a fixed
streaming interval would make this a tracked metric with trends in
`ledger.py`, rather than a one-off. That is the right home for the
before/after evidence any of these levers needs.

---

## Part 6: experiments, in priority order

Summarized here; the detailed plan -- method, deliverables, gate criteria,
tooling, and what gets merged back regardless of outcome -- is in
`re/streaming_overhead_experiments.md`.

**E1 -- Baseline the load properly.** The three-point protocol above, on
`x86_64-prod`, `arm64-prod` and `armhf-prod`, at all four rates. Deliverable:
a table of IRQ/s, `%sys+%irq+%soft`, and ns/callback. This is needed
regardless of which lever is chosen, and it is the missing quantification in
Frank's question. No driver change required.

**E2 -- Coalescing prototype.** First question, before any measurement: *does
this firmware accept a multi-packet bulk transfer at all?* Submit a single
2 x 512-byte OUT transfer and a multi-packet IN transfer and check they retire
normally. Only then build it out: sub-packet loop in both callbacks, N
configurable at build time, measured against E1's baseline at N = 1, 2, 4, 8.
Watch for: added xruns at small periods, MIDI OUT jitter, capture short-
transfer handling, and whether the watchdog's stall thresholds still make
sense. Existing hardware suite (`smoke`, then `regression`) plus a MIDI
round-trip latency check.

**E3 -- The on-demand gate question, and only this question.** With the device
streaming, stop the URBs, wait 10 s, restart them with *no* EP0 traffic, and
measure whether both directions resume and how long it takes. Then repeat
with `ploytec_start_streaming()` only, then with the full init. Also check
whether MIDI IN still arrives on EP 0x83 while the PCM URBs are stopped. This
needs a temporary development-only hook (a debugfs or module-parameter trigger
for stop/start), which project policy permits for bench validation provided it
is removed before submission and never ships. If a bare restart does not work,
**stop here** -- lever 3 is closed.

**E4 -- Idle downshift.** Only worth building after E1 quantifies what it
saves on the target platforms. Straightforward: a delayed work item taking
`rate_mutex`, cancelled by any PCM open, with the MIDI-activity question from
Part 3 settled one way or the other.

## What this study does not claim

- That the interrupt-per-URB model is universally valid. E1's full sweep
  (Part 1) confirms it closely on `x86_64-prod` and `arm64-prod` at every
  rate, and on `armhf-prod` at 44.1/48 kHz -- but on `armhf-prod` at
  88.2/96 kHz the observed rate runs 2-3x the flat model, most likely from
  recovery activity feeding back into the load that triggered it. The model
  holds below a saturation threshold and breaks above it; that threshold is a
  property of the host.
- That coalescing is free. The latency, period-size and ring-depth
  consequences in Part 2 are real and need measuring, not assuming.
- That lever 3 is impossible. It is unquantified and, after levers 2 and 4,
  low-value -- which is a different statement.
- Anything about `armhf` viability at high rates. Per the platform tier policy
  (`docs/test_strategy.md`), `armhf-prod`'s bar is "does not crash, hang, or
  oops", and none of these levers is being proposed to chase numbers there.
  pi1 is the *motivating observation*, not the target.
