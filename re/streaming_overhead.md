# Study: reducing the host overhead of continuous URB streaming

Options for cutting the CPU, interrupt and power cost the Jockey 3 imposes on
its host: transfer coalescing, idle rate downshift, and on-demand streaming.

**Status: E1 (baseline measurement) and E2a (firmware acceptance gate) done,
both on real hardware. E2a passed at N=2 in both directions -- see Part 2.
N=1/2/4/8 exploratory runs done at every N on both `x86_64-prod` and
`arm64-prod`; the reset/stall instability those exploratory runs found
turned out to be a driver bug (a watchdog timing check defeated by real
hardware restart-latency variance), now fixed -- a clean, untraced
N=1/2/4/8 re-run of `JT-RATE-001`/`JT-PERF-001` on both platforms is the
current picture (2026-08-27 section of Part 2).** The companion document
`re/streaming_overhead_experiments.md` is the executable plan for the
experiments this study calls for and tracks per-experiment status in more
detail.

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

### E2c preliminary result: `armhf-prod` (pi1test) -- the saturation feedback loop appears to be gone

Same comparison, on the board Part 1 flagged as the one where the flat
completion-rate model breaks down: N=2 run
(`tests/hw/results/armhf-prod/20260825T212251Z-smoke`) against the N=1
reference run already cited in Part 1's model-validation table
(`.../20260825T161240Z-smoke`; `155503Z` and `170435Z` are noisier N=1 runs,
the latter contaminated by the same post-96kHz restore-timeout condition
Part 1 already documents, and excluded here for that reason).

| point | `irq_per_s` N=1 | `irq_per_s` N=2 | cpu% N=1 | cpu% N=2 |
|---|---|---|---|---|
| unbound | 8,014.0 | 8,005.2 | 1.19 | 0.0 |
| idle/stream 44100 | 16,591.1 / 16,529.5 | 12,565.5 / 12,560.6 | 12.09 / 26.46 | 4.56 / 10.33 |
| stream 48000 | 17,176.4 | 12,875.3 (idle 12,927.7) | 62.69 | 27.81 (idle 11.22) |
| stream 88200 | **54,994.5** | **16,274.4** (idle 16,290.0) | 97.03 | 87.46 (idle 97.62) |
| stream 96000 | **92,565.2** | **17,044.4** (idle 17,212.1) | 95.16 | 97.12 (idle 96.53) |

(N=1's `idle` was a single pre-redesign measurement rather than per-rate,
hence the 44100-only comparison there; N=2 uses the current `idle_R`/
`stream_R` protocol throughout.)

**44.1/48 kHz show a real, non-trivial saving** -- roughly 2.3-2.6x on
`cpu_pct_sys_irq_soft`, larger than the ~24% `irq_per_s` drop alone would
suggest (pi1's fixed 8,000/s SOF-latch term dilutes the raw completion-rate
ratio, but the driver's own share of the CPU budget shrinks by more).

**88.2/96 kHz is the headline result.** At N=1 these were **2.0x and 3.1x**
the flat model's prediction (Part 1's "recovery-feedback loop" hypothesis:
a CPU too slow to service completions on schedule trips the watchdog,
which restarts or resets the URB ring, which is itself more interrupt
traffic, compounding the load that triggered it). At N=2, `irq_per_s` at
both rates comes back to within ~9% of the flat model
(`(packets/s / 2) + 8,000`) -- matching the well-behaved 44.1/48 kHz fit
instead of the runaway 2-3x multiplier. **The completion-rate reduction
appears to have broken the feedback loop, not just halved its input.** This
also matches the operational observation: this run finished in 314.1s,
against the 615.7s (with a runner hang after PASS) the N=1 baseline needed
on this same board.

**What N=2 does not fix:** the board is still fully CPU-saturated at
88.2/96 kHz (87-97%), it just no longer appears to be compounding that
saturation into extra recovery traffic. This is consistent with, not a
replacement for, the study's standing position that `armhf-prod`'s bar is
"does not crash, hang, or oops," not "stays fast."

**Confirmed against dmesg (2026-08-25, `run#20260825T212251Z`): zero stall,
resubmit-failure, or reset messages across the entire run**, enumeration
through all four rates and back down -- the only two driver log lines in
the whole capture are the firmware-version announcement at device attach
and a second one at the very end, from `restore_resting_rate()`'s normal
rate change back to 44.1 kHz, not a fault. At N=1 this exact sequence (a
sweep through 88.2/96 kHz on this board) reliably produced stalls,
`Failed to resubmit ... URB` bursts and resets (Part 1, and the earlier
`JT-AUDIO-002` wedge). **Coalescing looks like a genuine reliability
improvement here, not just a performance one.**

**Three more N=2 runs, same board, same day:** two came back byte-for-byte
as clean as the first (only the two firmware-version log lines). The third
hit two brief Playback stalls -- 25 ms and 24 ms, each self-recovered by the
watchdog's light URB restart in under 3 ms, with `substream open` (i.e.
during a `stream_R` point, not `idle_R`) -- and nothing else. **4 runs: 3
clean, 1 with two brief self-healing stalls, zero full USB resets in any
of them.** That is a different failure mode from N=1's, not just a rarer
one: N=1's stalls ran 400-500+ ms and needed a full reset to clear; N=2's
worst case so far self-heals in under 30 ms without ever escalating.
Coalescing has not been shown to eliminate watchdog activity on this board
outright, but every occurrence observed so far stayed inside the *light*
recovery path, never reaching the *hard* one -- four runs is still a small
sample, not a validated pattern, but the picture is consistent, not mixed.

### E2c preliminary result: minimum period doubles at N=2, MIDI OUT throughput unaffected

On `x86_64-prod`, confirming the two predictions the touch-point list made
in advance -- and repeated on `arm64-prod` with the same result to within a
few bytes/sec (`JT-PCM-007`: 240/288 B minimum, same as `x86_64-prod`;
`JT-MIDI-004`: 2496-2497 B/s at all four rates, same as `x86_64-prod`).
Both effects are platform-independent, as expected -- neither depends on
host CPU or interrupt behavior, just on the sub-packet/period-size
arithmetic and the MIDI rate limiter, both fixed by the protocol and the
driver's own constants regardless of host.

**`JT-PCM-007` (achievable latency sweep) confirms the minimum period grows
with N, exactly as predicted.** 120 B (playback) and 144 B (capture) --
the pre-coalescing minimum, still what the test's own legal range assumes
-- are now refused by `hw_params()` (`period_bytes_min` is now
`N x subpacket_bytes`, per the E2b implementation). The smallest period
that succeeds is exactly double: 240 B / 0.454 ms playback, 288 B / 0.363
ms capture -- matching 20/16 frames at 44.1 kHz, against 10/8 frames
(0.227/0.181 ms) at N=1. The test reports this as a FAIL because its
expectations predate coalescing, not because anything behaved incorrectly;
it needs updating for N=2 rather than the driver needing a fix. This is
the real, quantified cost side of the ledger: **the minimum achievable
latency roughly doubles**, which any latency-sensitive application
negotiating a small period will feel directly.

**`JT-MIDI-004` (MIDI OUT throughput per rate) confirms the leaky-bucket
fix holds.** 2496-2497 B/s achieved against a 2500 B/s target at all four
rates -- within the same +/-5% band N=1 always measured, no throughput
loss from N=2. This is the direct validation of pulling
`jockey3_get_next_midi_out_byte()` once per sub-packet rather than once
per URB (E2b's implementation section): had that been wrong, this is
exactly where it would have shown up, as throughput divided by roughly
`JOCKEY3_PLAYBACK_N`.

### E2 exploratory: N=4 on `arm64-prod` -- post-rate-change stalls are frequent, but every one self-recovers

N=4 (both directions) is not yet gated by an E2a-style acceptance probe --
E2a only tested N=2 -- and this is the first look at it, on `arm64-prod`.
`JT-MIDI-004` passed clean (2496-2497 B/s MIDI throughput, same as N=2).
Audio verified clean by ear. Firmware accepts the 4x512B (2048B) transfer
without incident.

**`JT-PCM-007` passed, but not exactly as the simple model predicts.**
Capture's clean minimum is 576 B (32 frames) -- exactly `N x
subpacket_bytes`, matching the model precisely, same as N=2's exact match
at 288 B. **Playback's theoretical minimum, 480 B (`period_bytes_min` = 4
x 120), is legally accepted by `hw_params()` but produced 1 xrun in
practice** -- the actual clean minimum needed doubling to 960 B (80
frames, 1.814 ms). At N=2 the theoretical minimum (240 B) *was* the clean
minimum, with zero xruns right at the boundary. **This extra playback-only
headroom requirement is new at N=4, not present at N=2** -- whatever
margin `period_bytes_min`'s simple `N x subpacket_bytes` formula assumes
is sufficient, it is not, for playback, once N reaches 4.

**But `JT-AUDIO-002` and `JT-RATE-001` both show a real, consistent pattern:
a brief Playback stall, always self-recovered, after a large fraction of
rate changes.** Counted directly from the two runs' full dmesg:

- `JT-AUDIO-002` (4 rate changes): 1 stall -- 25%.
- `JT-RATE-001` (60 rate changes): 16 stalls -- **26.7%**, tightly
  clustered (detected at 20-23 ms, always just past
  `JOCKEY3_WATCHDOG_STALL_MS`; recovered at 24-26 ms). Zero Capture
  stalls. Zero escalations to a full reset -- every single one recovered
  on the watchdog's first light URB restart.

That is a large jump from arm64-prod's own documented N=1 rate-change
baseline: 12 playback stalls out of 4,000 changes (0.3%) from the
Milestone 13 endurance run (`re/rate_change_stall.md`). **Coalescing
appears to make this board hit its light watchdog-restart path roughly
two orders of magnitude more often right after a rate change, at N=4.**
Whether this is N=4-specific or already present, less visibly, at N=2 is
not yet established -- the earlier N=2 `JT-AUDIO-002` run on this board
also showed one stall in its 4 rate changes (also 25%), which in hindsight
is not obviously consistent with the 0.3% N=1 baseline either, but that
run's dmesg was not saved with a comparable rate-change count to N=4's
60-change `JT-RATE-001` run, so this is not yet a controlled comparison.

**This does not (yet) look like a reliability regression** -- every
occurrence stayed on the light, self-healing path the same way
`armhf-prod`'s did, and nothing here contradicts the E2b finding that the
underlying playback-stall mechanism itself is pre-existing (Milestone 13),
not introduced by coalescing. What has changed, and needs an honest
answer before recommending any N: **how much more often that pre-existing
mechanism triggers as N grows.** A `JT-RATE-001`-scale run (dozens of rate
changes, full dmesg) at N=1 and N=2 on the same board would settle whether
this is proportional to N, a step change at some threshold, or was already
this frequent and simply never sampled at this scale before.

### E2 exploratory: N=4 on `x86_64-prod` -- lower stall frequency, but a real escalation, and CPU% moves the wrong way

**The period-headroom finding above is confirmed cross-platform, not
arm64-specific.** `JT-PCM-007` on `x86_64-prod` shows the identical
pattern byte-for-byte: capture clean at 576 B (matches the model
exactly); playback's theoretical 480 B legally accepted but 1 xrun,
actual clean minimum 960 B. Whatever is eating that extra headroom is a
property of N=4 itself, not this one board.

**Stability looks different here than on `arm64-prod`, in both directions
at once.** Counted from full dmesg:

- `JT-MIDI-004`: the very first rate change produced a Playback stall that
  self-recovered (23 ms), then a second stall on the same direction that
  did **not** clear on the watchdog's first restart -- `Playback URB has
  stalled` twice more, then `queuing full USB reset`, then a real
  `usb ... reset high-speed USB device`.
- `JT-PCM-007`: one Capture stall (substream idle), self-recovered in 56 ms,
  nothing else.
- `JT-RATE-001` (100 rate changes): only **4 stalls -- 4%**, a fraction of
  `arm64-prod`'s 26.7% on the same test. But **one of the four escalated
  to a full reset** (Capture, substream open, the very first stall in the
  run) -- something that never happened once across `arm64-prod`'s 16.
  The other three (all Playback, substream idle) self-recovered in
  53-58 ms.

**So the two boards diverge in opposite directions**: `arm64-prod` stalls
far more often but always self-heals; `x86_64-prod` stalls rarely but,
when it does, has a real chance of needing the hard recovery path.
Neither board shows this at N=2. Two boards, two different failure
signatures, both novel at N=4 -- not something a single-platform read
would have caught.

**`JT-PERF-001` adds a third divergence: CPU% does not keep improving on
`x86_64-prod` the way it does on `arm64-prod`.** `irq_per_s` still halves
against the N=2 baseline at 3 of 4 rates (0.500-0.501); 44.1 kHz is an
outlier at **0.404**, not explained yet. But `cpu_pct_sys_irq_soft` while
streaming *increases* from N=2 to N=4 at every rate --

| Rate | irq ratio N=2->N=4 | cpu% stream N=2->N=4 | cpu% idle N=2->N=4 |
|---|---|---|---|
| 44100 | 0.404 | 1.07 -> 2.30 | 0.17 -> 0.12 |
| 48000 | 0.501 | 0.68 -> 1.92 | 0.12 -> 0.07 |
| 88200 | 0.500 | 1.25 -> 1.72 | 0.12 -> 0.15 |
| 96000 | 0.500 | 1.07 -> 1.45 | 0.15 -> 0.15 |

-- while `idle` CPU stays flat or drops, as expected. `arm64-prod` shows
the opposite pattern at the same transition (irq ratio a clean 0.500-0.501
at all four rates; `stream` CPU *continues to drop*, e.g. 96 kHz
1.01% -> 0.87%). **On `x86_64-prod`, N=4 is not a clear further win over
N=2 in CPU terms while a stream is actually open, even though completion
count keeps falling.** This `JT-PERF-001` run followed directly after the
stall/reset-heavy runs above, on the same device session -- whether that
instability contaminated this specific measurement, or N=4 genuinely costs
more per-open-stream CPU on this host, is not yet known. One run; needs
repeating before drawing a conclusion either way.

### E2 exploratory: N=8 on `arm64-prod` -- clean CPU/irq trend continues, stall rate is lower than N=4, not higher

Audio clean by ear. `irq_per_s` halves again cleanly against the N=4
baseline at all four rates (0.500 ratio, no anomaly this time).
`cpu_pct_sys_irq_soft` continues the same well-behaved trend N=2->N=4
already showed on this board -- flat or improving at every rate, both
`idle` and `stream` (e.g. 96 kHz stream: 0.87% at N=4 -> 0.40% at N=8).
No reversal here, unlike `x86_64-prod`'s N=2->N=4 CPU anomaly.

**`JT-RATE-001` (60 rate changes): 10 stalls -- 16.7%, all self-recovered,
zero escalations** (9 Playback, 1 Capture). **That is LOWER than N=4's
26.7% on the same board, not higher.** The stall rate is not simply
increasing with N -- whatever drives it is not monotonic, at least not
between N=4 and N=8. `JT-AUDIO-002` (4 rate changes) came back fully
clean, which is unsurprising noise at this sample size against either a
17% or 27% true rate.

**`JT-PCM-007` needs a caveat about what it can actually show.** Both
directions' theoretical minimum (960 B playback = 8x120, 1152 B capture =
8x144) tested clean, zero xruns. But the test's candidate ladder is a
fixed sequence of frame-count doublings (10/20/40/80 for playback,
8/16/32/64 for capture) that was never designed around a variable N --
it only happens to land exactly on `N x subpacket_bytes` because N itself
is a power of two here, same as at N=4. Critically, **960 B is the exact
same absolute period size** that N=4 already proved clean (as the
"doubled" safe candidate there) -- this run does not newly show N=8's
own theoretical boundary is more forgiving than N=4's was; it re-tests a
size already known to be safe. The ladder has no rung between N=8's own
period_bytes_min and the next doubling, so whether N=8's *own* tight
boundary carries the same single-xrun marginal risk N=4's boundary showed
is genuinely untested here, not disproven. `JT-PCM-007`'s candidate
ladder would need to scale with N (or add finer steps) to actually answer
that.

### E2 exploratory: N=8 on `x86_64-prod` -- the completion-rate model itself starts breaking down, and reset frequency is climbing

Audio clean by ear. `JT-PCM-007` and the initial `JT-AUDIO-002`/
`JT-PERF-001` runs came back clean (same period-headroom-ladder caveat as
`arm64-prod`'s N=8 result applies here too).

**`irq_per_s` no longer halves cleanly against the N=4 baseline, at any
rate, and `idle` and `stream` diverge from each other for the first time
in this whole study:**

| Rate | idle ratio N=4->N=8 | stream ratio N=4->N=8 | idle-vs-stream gap at N=8 |
|---|---|---|---|
| 44100 | 0.554 | 0.534 | 1.3% |
| 48000 | 0.447 | 0.452 | 1.0% |
| 88200 | 0.399 | 0.412 | 3.4% |
| 96000 | 0.398 | 0.470 | **18.0%** |

Every prior N transition on every platform, including `idle` vs `stream`
matching almost exactly (the whole point of "URBs run free regardless of
PCM open/close"), held to within a percent or two of the simple model.
None of that holds here. This `JT-PERF-001` run's own dmesg is completely
clean (no stalls, no resets) -- so this is not recovery activity skewing
the numbers; something about actual URB completion behavior itself
changes at N=8 on this board, and per-callback processing time did grow
(`ns_per_capture_cb` stream vs idle up 34-38%, `ns_per_playback_cb` up
7-14%, from real encode/decode work replacing the idle path) but that
does not obviously explain a *higher* stream completion rate at 96 kHz --
slower per-completion processing would be expected to reduce throughput,
not raise it. **Not understood yet; flagged rather than explained.**

**`JT-RATE-001` (100 rate changes): 8 stalls -- 8%, up from N=4's 4% --
and 3 of those 8 escalated to a full USB reset (37.5% of stalls, up from
N=4's 1 of 4 = 25%).** All 8 were Playback, all "substream idle". Two of
the three resets followed a transition *into* 96000 Hz specifically; the
third followed a transition into 88200 Hz. The last reset's log carries
an extra line the others didn't: `Rate change to 96000 Hz left a stream
stalled (playback_alive=0, capture_alive=0, capture_open=1)` -- both
directions found dead at once, the same combined-failure signature
Frank's original `JT-AUDIO-002`/`JT-MIDI-001` investigation found at N=1
(E2b section, above).

**x86_64-prod's stability is trending the wrong way as N grows, and
monotonically so, unlike `arm64-prod`'s non-monotonic stall rate:** 0
issues of any kind at N=2 (every test), 4% stalls / 1 reset at N=4, 8%
stalls / 3 resets at N=8. `arm64-prod` shows no equivalent trend (26.7%
at N=4, 16.7% at N=8, zero resets at either). Whatever is driving this on
`x86_64-prod` is getting more frequent and more severe with N, which
`arm64-prod`'s data does not show at all -- this is the strongest signal
so far against simply picking the largest N that "works."

### 2026-08-27: the N-vs-stability trend above was a driver bug, now fixed -- full N=1/2/4/8 sweep is clean

**The instability the sections above tracked -- worsening on `x86_64-prod`
as N grew, always self-healing but non-monotonic on `arm64-prod` -- was not
an inherent cost of higher N. It was `jockey3_watchdog_check()`'s startup
classification being defeated by real, N=1-and-up restart-latency variance,
root-caused and fixed this same day; see `re/rate_change_stall.md`'s three
2026-08-27 follow-ups for the full mechanism (a small hardware-side FIFO
lets 1-2 early URB completions through before the device is actually ready,
confirmed on the wire independent of this driver) and the fix (every
"has this direction reached steady streaming yet" check consolidated onto
one time-based budget, `JOCKEY3_STREAM_STARTUP_GRACE_MS`).**

With that fix in place, `JT-RATE-001` was re-run at all four N values, both
platforms, no bpftrace/trace_printk attached (kept out so this run's timing
is directly comparable to earlier untraced runs). Every run came back
clean -- 0 resets, effectively 0 stalls -- except one: `x86_64-prod` at N=2
had a single reset on the run's very first rate change after probe,
`stalls_direction_first=1`, the framework's own pre-existing bucket for
"first change after probe" (see `cases/rate_change.py`, which tracks this
direction separately for exactly this reason). Isolated, not repeated
anywhere else in that run or in N=1/N=4/N=8 on either platform -- read as a
one-off cold-start condition, not an N=2 regression.

**This resolves the open question the N=4/N=8 exploratory sections above
raised.** The pre-fix data is kept as-is above (append-only, per this
document's convention) because the reasoning that found the fix is worth
keeping, but its conclusion -- "this is the strongest signal so far against
simply picking the largest N that works" -- no longer holds against
current `jockey3.c`.

**`JT-PERF-001`, same runs, consolidated into one table per target.** State
= idle (URBs flowing, MIDI only) vs. stream (a substream open at that
rate); `cpu%` is `cpu_pct_sys_irq_soft` (softirq + system time attributable
to the driver's IRQ/URB path):

**`x86_64-prod`**

| State | irq/s N=1 | N=2 | N=4 | N=8 | cpu% N=1 | N=2 | N=4 | N=8 |
|---|---|---|---|---|---|---|---|---|
| idle 44.1 kHz | 9,918.7 | 4,960.0 | 2,006.8 | 1,140.0 | 0.65 | 0.40 | 0.20 | 1.32 |
| stream 44.1 kHz | 9,926.0 | 4,958.6 | 2,235.3 | 1,137.7 | 1.02 | 0.90 | 2.30 | 1.50 |
| idle 48 kHz | 10,790.4 | 5,384.5 | 2,313.9 | 1,214.9 | 0.32 | 0.40 | 1.88 | 0.98 |
| stream 48 kHz | 10,481.6 | 5,388.3 | 2,586.8 | 1,238.7 | 1.40 | 1.30 | 2.27 | 1.32 |
| idle 88.2 kHz | 19,855.1 | 9,927.4 | 4,678.9 | 1,981.8 | 3.13 | 0.57 | 2.37 | 0.18 |
| stream 88.2 kHz | 19,853.8 | 9,908.5 | 4,881.0 | 2,056.7 | 3.35 | 1.20 | 1.88 | 2.18 |
| idle 96 kHz | 21,542.6 | 10,770.1 | 5,400.1 | 2,235.2 | 3.43 | 1.05 | 0.12 | 0.50 |
| stream 96 kHz | 21,538.0 | 10,799.6 | 5,395.4 | 2,265.4 | 3.68 | 1.57 | 0.92 | 1.57 |

**`arm64-prod`**

| State | irq/s N=1 | N=2 | N=4 | N=8 | cpu% N=1 | N=2 | N=4 | N=8 |
|---|---|---|---|---|---|---|---|---|
| idle 44.1 kHz | 9,908.2 | 4,949.9 | 2,482.4 | 1,241.2 | 0.57 | 0.22 | 0.15 | 0.15 |
| stream 44.1 kHz | 9,928.9 | 4,949.8 | 2,482.4 | 1,241.2 | 0.84 | 0.30 | 0.27 | 0.25 |
| idle 48 kHz | 10,791.3 | 5,388.7 | 2,700.7 | 1,350.3 | 0.60 | 0.20 | 0.25 | 0.22 |
| stream 48 kHz | 10,795.4 | 5,387.3 | 2,700.5 | 1,350.2 | 0.74 | 0.74 | 0.17 | 0.20 |
| idle 88.2 kHz | 19,856.3 | 9,928.2 | 4,964.7 | 2,482.4 | 0.55 | 0.45 | 0.20 | 0.22 |
| stream 88.2 kHz | 19,847.9 | 9,928.3 | 4,964.6 | 2,482.4 | 1.68 | 1.24 | 0.40 | 0.42 |
| idle 96 kHz | 21,562.6 | 10,800.3 | 5,400.2 | 2,700.5 | 0.82 | 0.40 | 0.25 | 0.25 |
| stream 96 kHz | 21,559.0 | 10,800.9 | 5,400.3 | 2,700.6 | 1.85 | 0.79 | 0.77 | 0.59 |

`arm64-prod`'s `irq/s` halves cleanly at every single N-doubling and every
rate (ratio 0.500 throughout, to 3 significant figures) -- the flat
per-URB model holds exactly on this board across the whole N range now
tested. `x86_64-prod` keeps the irregularity the exploratory sections
above already flagged (ratios from 0.40 to 0.57 rather than a clean 0.5),
but the specific N=4->N=8 anomaly called out there -- `idle` and `stream`
diverging from each other, up to 18.0% apart at 96 kHz -- has shrunk
sharply: recomputed from this table, the same comparison is now 0.2-3.6%
across all four rates. Whether that's the watchdog fix removing
recovery-adjacent noise from what was being measured, or just this run's
own noise floor, isn't established -- flagged, not explained, consistent
with how this document has treated `x86_64-prod`'s irq/s irregularity
throughout.

`cpu%` does not fall monotonically with N on either board -- both show
values that go up as well as down moving from one N to the next (e.g.
`x86_64-prod` stream 44.1 kHz: 1.02 -> 0.90 -> 2.30 -> 1.50). Read
`cpu_pct_sys_irq_soft` at this resolution as noisy in absolute terms; the
`irq/s` trend is the reliable signal, `cpu%` is directional at best from a
single run per N.

**Per-callback processing time (`playback_cb`/`capture_cb`, ns) grows with
N on both platforms** -- expected, since a coalesced URB's completion
handler processes N sub-packets in one call, and confirms the mechanism
the N=8 `x86_64-prod` exploratory section above first noticed:

**`x86_64-prod`**

| State | playback_cb ns N=1 | N=2 | N=4 | N=8 | capture_cb ns N=1 | N=2 | N=4 | N=8 |
|---|---|---|---|---|---|---|---|---|
| idle 44.1 kHz | 1,504 | 2,831 | 11,560 | 10,973 | 1,368 | 2,318 | 9,574 | 8,478 |
| stream 44.1 kHz | 1,450 | 2,670 | 11,232 | 14,247 | 1,616 | 2,983 | 10,858 | 13,561 |
| idle 48 kHz | 1,505 | 2,573 | 9,606 | 12,339 | 1,371 | 2,256 | 8,288 | 9,864 |
| stream 48 kHz | 2,267 | 2,531 | 8,054 | 12,848 | 2,518 | 3,065 | 8,382 | 12,228 |
| idle 88.2 kHz | 2,236 | 1,581 | 6,738 | 11,948 | 1,976 | 1,357 | 6,646 | 9,188 |
| stream 88.2 kHz | 2,205 | 1,594 | 5,908 | 12,828 | 2,414 | 1,715 | 6,362 | 12,136 |
| idle 96 kHz | 2,270 | 1,680 | 3,987 | 10,359 | 2,016 | 1,446 | 2,243 | 8,395 |
| stream 96 kHz | 2,269 | 1,533 | 3,094 | 12,744 | 2,454 | 1,654 | 3,980 | 12,613 |

**`arm64-prod`**

| State | playback_cb ns N=1 | N=2 | N=4 | N=8 | capture_cb ns N=1 | N=2 | N=4 | N=8 |
|---|---|---|---|---|---|---|---|---|
| idle 44.1 kHz | 8,490 | 7,705 | 11,363 | 11,209 | 8,093 | 6,610 | 8,425 | 6,828 |
| stream 44.1 kHz | 9,668 | 11,317 | 13,746 | 18,362 | 11,093 | 12,278 | 14,337 | 21,210 |
| idle 48 kHz | 6,581 | 7,205 | 11,390 | 14,146 | 6,263 | 6,170 | 8,346 | 8,634 |
| stream 48 kHz | 6,945 | 11,149 | 13,578 | 18,151 | 8,021 | 12,194 | 14,444 | 21,019 |
| idle 88.2 kHz | 4,322 | 7,187 | 8,202 | 14,181 | 4,054 | 6,138 | 6,012 | 8,592 |
| stream 88.2 kHz | 7,201 | 11,008 | 13,942 | 18,606 | 8,280 | 12,451 | 14,533 | 21,237 |
| idle 96 kHz | 6,612 | 7,428 | 8,305 | 14,070 | 6,311 | 6,354 | 6,114 | 8,591 |
| stream 96 kHz | 7,201 | 8,294 | 13,861 | 17,930 | 8,306 | 9,095 | 14,195 | 21,370 |

`arm64-prod`'s `stream` callback cost grows the most cleanly with N (e.g.
96 kHz playback: 7,201 -> 8,294 -> 13,861 -> 17,930 ns) -- consistent with
per-sub-packet codec work dominating on the weaker core, exactly where
Part 1 already expected `arm64-prod` to show it. This is real per-open-
stream CPU cost, not recovered by `irq/s` falling -- the two `cpu%` tables
together are the actual trade a higher N makes: fewer, more expensive
callbacks. Whether that nets out favorably is a per-platform, per-rate
question this table answers directly rather than the flat completion-rate
model alone.

### 2026-08-27: next step is E2d -- N chosen per PCM open, not compile-time

With coalescing now stable at every N and the per-N trade above being a
genuine per-platform/per-rate question with no single obviously-right
answer, the work moves to deriving N per stream open from the requested
period size (largest power of two that fits, `1 <= N <= 8`, per direction,
carried as a shift count). The full decision record -- why it is period
size and not buffer size that binds, why no new `hw_constraint` is needed,
the power-of-two/`n_shift` representation, and the collision with the
"URBs run for the device's lifetime" model (shrinking N is mandatory,
growing it is only an optimization) -- is in
`re/streaming_overhead_experiments.md`'s **E2d** section.

That section also holds **E2d-exp**, the one experiment that gates the
design: whether the firmware tolerates `transfer_buffer_length` changing
between resubmissions of an already-running ring. If it does, runtime N
never tears the ring down; if it does not, N can only grow when the ring is
being rebuilt anyway. Patch written (temporary `exp_pb_n`/`exp_cap_n`
module parameters), hardware run deferred to 2026-08-28.

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

### 2026-08-28: E2 complete -- N chosen per PCM open, implemented and hardware-validated

The "next step" above has been taken and closed. `re/streaming_overhead_experiments.md`'s
**E2d** section has the full design record and the **E2d-exp** result (runtime
`transfer_buffer_length` changes need no ring teardown -- confirmed on
`x86_64-prod` hardware, for host-side/USB-core reasons rather than a firmware
accommodation, since `PLOYTEC_PKT_SIZE` is exactly the USB high-speed bulk
max-packet size). Its "Implementation status" subsection lists everything
that landed in `jockey3.c` on `dev/streaming-overhead`.

**This changes how the two sections above should be read.** They analyze a
single, compile-time N=8 applied to every stream. That is not what shipped.
`jockey3_pcm_set_n()` derives N from the requested period size on every
`hw_params()` call, per direction: `1 <= N <= 8`, largest power of two with
`N * pkt_bytes <= period_bytes`. A small-period client (the common case for
low-latency work) lands on N=1 or N=2 and pays none of the "What it costs"
figures above -- those apply only to a client whose period size is large
enough to actually earn N=8. The completions/s table is still the correct
number *for the N=8 case*; it is no longer *the* number, because N=8 is now
one outcome among four rather than the only one.

The specific risks the "What it costs to implement" section flagged were
each resolved as anticipated: `period_bytes_min` was relaxed to the N=1
values (120/144 bytes) rather than left at the old N=8 floor, and
`jockey3_check_urb_stream_alive()`'s liveness window is no longer a
hardcoded `NSEC_PER_MSEC` -- `JOCKEY3_LIVENESS_WINDOW_NS(shift)` scales it
with the direction's live `n_shift`, so the "must be at least one URB span"
rule holds at whatever N that direction actually negotiated, not just at
N=8. Ring depth was left at 8 URBs; since N is now chosen per open rather
than fixed, the 64-packets-in-flight worst case only occurs for a client
that specifically negotiates a large enough period to get N=8, and revisiting
ring depth did not come up as necessary during implementation or hardware
validation.

**Hardware validation:** `JT-PCM-010` (`tests/hw/cases/pcm_n_sweep.py`) --
real sustained transfers at every period size on the ladder
`1, 2, 3, 4, 5, 7, 8, 12, 16` packets, verifying via a gated `dev_dbg` that
the driver actually chose the expected N (not just that the transfer ran
xrun-free) -- came back clean on both `x86_64-prod/20260828T174506Z-functional`
and `arm64-prod/20260828T175023Z-functional`: 9/9 candidates N-correct in
both directions, on both platforms, no `EPROTO`/unexpected driver
messages/oops in either run's `dmesg.txt`, xruns only at the tightest N=1
buffer (metrics-only, not a failure criterion here -- see
`jockey3-audio-test-gain-invariance`-style reasoning: absolute performance
at the tightest legal buffer is host-dependent, not a driver correctness
question).

**What this validation does not cover:** the completions/s and `cpu%`/irq
tables above are from the earlier fixed-N=1/2/4/8 sweep, built with N as a
compile-time constant for the whole run. `JT-PCM-010` proves N *selection*
is correct and that streaming survives at every N the runtime mechanism can
choose -- it does not re-run `JT-PERF-001` against the runtime-N build, so
the CPU/interrupt savings figures above are not re-confirmed on the shipped
mechanism itself. Nothing found during implementation or validation suggests
they would differ (a URB carrying N packets looks identical on the wire and
to the completion handler regardless of whether N was chosen at compile time
or at `hw_params()` time), but this is an assumption, not a measured claim.
Re-running `JT-PERF-001` on the runtime-N build would close that gap if the
exact savings-at-N=8 number is needed again for something.

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

> **2026-08-29: lever 3 is being pursued after all**, on a narrower
> justification than this Part assumes -- and notably *without* the L0/power
> benefit Part 5 point 5 attributes to it, because MIDI IN stays submitted
> across the idle period. `re/on-demand_streaming.md` is the working document
> for the feature and supersedes experiment E3. The analysis below is kept as
> written; where the two disagree, the newer document says so explicitly.

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

**2026-08-29: it is being pursued** -- see `re/on-demand_streaming.md`. The gate
question changed shape: restarting is deliberately treated as a full cold start
rather than being held to a 20 ms budget, so the only gate left is whether MIDI
IN survives on EP 0x83 while the PCM URBs are stopped. That one is still
terminal.

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
