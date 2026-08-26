# Experiment plan: reducing the host overhead of continuous URB streaming

The executable plan for the experiments called for in
`re/streaming_overhead.md`. That document is the analysis and holds the
reasoning; this one is what gets done, in what order, with what exit criteria.

Read the study first. In short: the driver submits one 512-byte Ploytec packet
per URB, which fixes the URB completion rate at 9,923/s at 44.1 kHz and
21,600/s at 96 kHz for as long as the device is plugged in. Four levers exist
-- status quo, idle rate downshift, on-demand start/stop, and transfer
coalescing -- and coalescing looks like roughly a 9x reduction with no
deviation from the vendor-validated continuous-stream model.

## Working method

**Code work happens on `dev/streaming-overhead`**, branched from `main`.
Prototypes there are allowed to be throwaway: a build-time `N`, a debugfs
trigger, a hardcoded target, whatever answers the question fastest.

**Findings land on `main` regardless of what happens to the code.** This is
the part that must not be lost. Every experiment below names a *documentation*
deliverable separately from its code deliverable, and the documentation
deliverable is committed to `main` (directly, or cherry-picked from the
branch) as soon as the experiment concludes -- including when it concludes
"this does not work". A negative result that is written down is worth more
than a prototype that is not.

Concretely, per experiment:

| | Lands on `main` | Lives on the branch |
|---|---|---|
| E1 | the `JT-PERF-001` case + the baseline numbers | -- |
| E2 | the measured result either way; the driver change only if it wins | the prototype |
| E3 | the answer to the gate question, written up | the dev-only hook, never merged |
| E4 | the measured result; the driver change only if it wins | the prototype |

**Order matters.** E1 is a prerequisite for E2 and E4 -- without a baseline
there is nothing to compare against, and the study's own load model has been
checked against exactly one clean data point. E3 is independent and optional.

**Hardware runs are Frank's to schedule.** Long runs go overnight. E1 in
particular needs an otherwise-idle machine, the same constraint `JT-CODEC-005`
already carries.

**`-prod` configs only** for any performance number. A KASAN/debug kernel
makes the device crackle audibly while every ALSA counter stays clean
(`docs/debug_kernel_audio_quality.md`); numbers taken there are void.

---

## E1 -- Baseline the load properly

**Status: tooling implemented and merged to `main` (`572b677`), not this
study's branch.** `JT-PERF-001` / `cases/perf_baseline.py` is general-purpose
regression-monitoring infrastructure -- the natural way to notice if a future
change makes the driver's host cost worse, independent of anything in this
study -- so it lives on `main` rather than `dev/streaming-overhead`, unlike
E2-E4 below which are about this study's own levers. Smoke-tested end to end
on `alsa-test`: idle and streaming completion rates already match the model
in `re/streaming_overhead.md` almost exactly (idle 9927.5/s vs. 9,923
theoretical at 44.1 kHz; streaming 19854.4/s vs. 19,845 theoretical at
88.2 kHz) -- see the case's own commit messages for the full readout. Not yet
run as a proper baseline sweep across targets; that run is still open.

**Question:** what does the Jockey 3 actually cost its host, per platform, per
rate, idle and streaming?

**Why first:** it is the quantification the whole study is missing. The only
usable measurement on record is a single clean `vmstat` reading at 44.1 kHz on
`pi1test`; the 88.2 kHz reading was taken during the `JT-AUDIO-002` wedge and
measures a fault, not a baseline. Every later experiment is a before/after
against this.

**No driver change.** E1 is pure measurement against the current driver.

### The three-point protocol

Always in this order, on the same boot, with the machine otherwise idle:

1. **Device unplugged** -- the host's own floor. On `pi1test` this is what
   subtracts the dwc2 SOF latch at 8,000/s; without it no other number means
   anything.
2. **Plugged and idle** -- driver bound, URBs running, nothing open. This is
   the cost levers 2, 3 and 4 attack.
3. **Streaming** -- playback + capture open, at each of 44100, 48000, 88200,
   96000 Hz.

### What to sample at each point

| Metric | How | Why |
|---|---|---|
| HCD interrupts/s | delta on the `xhci_hcd` / `dwc2` line of `/proc/interrupts` over a fixed interval | device-attributable, unlike `vmstat`'s aggregate `in` |
| CPU time | `mpstat -P ALL 1`, summing `%sys + %irq + %soft` | the portable metric; comparable across hosts and across coalescing factors |
| ns per callback | ftrace `funcgraph` or bpftrace histogram on `jockey3_playback_callback` / `jockey3_capture_callback` | separates per-URB overhead (which coalescing removes) from codec work (which it does not) |
| C-state residency | `turbostat` (x86) / `powertop` | the power cost, invisible in an IRQ count; the strongest argument for levers 2 and 4 on a laptop |

**Caveat to record with every IRQ figure:** xHCI interrupt moderation means
IRQ count is not completion count. IRQ/s is comparable *within* a host, never
*between* hosts. Where the two disagree, CPU time is the number to trust.

The ftrace/bpftrace measurement uses standard kernel tracing against
unmodified source. No driver test hooks -- the standing rule holds.

### Tooling

A new metric-only case, `JT-PERF-001`, modeled on `JT-CODEC-005`:

- new area `PERF` in `catalog.yaml` (`areas:` block) -- "Host CPU, interrupt
  and power cost of the driver"
- `level: L3`, `mode: automated`, `per_target: true`, `status: implemented`
- `exec: cases/perf_baseline.py`, params for interval length and the rate list
- metrics: `irq_per_s_unplugged`, `irq_per_s_idle`, `irq_per_s_stream`,
  `cpu_pct_idle`, `cpu_pct_stream`, `ns_per_playback_cb`, `ns_per_capture_cb`
- `pass:` metric-only, like `JT-CODEC-005`
- **not in any profile** -- needs an idle machine, invoked with
  `--case JT-PERF-001`
- new `chmod +x cases/perf_baseline.py`, or `runner.py` fails with Errno 13

The unplugged point needs the device de-energized or unbound; where the target
has switched-power capability the existing `lib/power` abstraction covers it,
otherwise the case prompts the operator (semi-automated) rather than skipping
the point.

### Platforms

`x86_64-prod` and `arm64-prod` are the primary targets. `armhf-prod`
(`pi1test`) is the motivating observation and worth one run for the record,
but per the platform tier policy it is not a target being optimized for --
nothing here is being tuned to chase numbers on that board.

### Deliverable

- **Code (main):** `cases/perf_baseline.py`, the `PERF` area and the
  `JT-PERF-001` catalog entry.
- **Documentation (main):** a results table in `re/streaming_overhead.md`
  replacing Part 1's single-data-point validation, plus whether the
  interrupt-per-URB model holds up across hosts.

**Exit criterion:** a baseline table for at least `x86_64-prod` and
`arm64-prod`, all four rates, all three points.

---

## E2 -- Transfer coalescing

**Question:** does N x 512 bytes per bulk URB work on this firmware, and what
does it save?

This is the lever with the largest expected payoff -- roughly 89% off the
completion rate at every sample rate -- and the one that does not deviate from
the vendor model, because the device paces the wire itself and cannot tell the
difference (study, Part 2).

### E2a -- the firmware acceptance probe (gate)

**Before any refactoring.** The Ozzy precedent shows the Ploytec 512-byte
sub-packet framing tolerates multi-packet transfers on *related* devices at a
different vendor ID with a different channel count. It does not establish that
Jockey 3 firmware accepts a 4096-byte bulk OUT.

Smallest possible test: raise the transfer length of a single playback URB to
2 x 512 and a single capture URB to 2 x 512, leave everything else alone, and
check that they retire with the full `actual_length`, that the device keeps
streaming, and that audio is unbroken. Then 4x, then 8x.

**Gate:** if the device stalls, short-transfers, or wedges at N=2, coalescing
is closed and E2 stops here. Write that up -- it is a first-class finding
about this firmware and belongs in `main` whatever happens next.

**Watch for** a short IN transfer: the device may return one sub-packet and
retire the URB early. That is not necessarily failure -- it is a design
constraint (capture must handle `actual_length / PLOYTEC_PKT_SIZE`
sub-packets, not blindly N) -- but it changes the arithmetic if it happens
routinely.

**Result: PASSED at N=2, both directions.** Tested on `alsa-test` hardware,
`dev/streaming-overhead` (`JOCKEY3_E2A_COALESCE_PROBE`, never merges to
`main`). Capture returned full `actual_length=1024` (no short transfers) for
35+ seconds continuously across two runs. Playback showed one isolated
self-recovered stall on the first run, not reproduced on a second, longer
run that completed with zero kernel messages. Full writeup in
`re/streaming_overhead.md`, Part 2 ("E2a result"). N=4 and N=8 not yet
probed -- do not assume clean on this evidence. Next: E2b.

### E2b -- the prototype

`N` configurable at build time, separately per direction. The per-packet
processors are already separable, so the change is mostly mechanical:

```c
for (i = 0; i < n_subpkts; i++)
        period_elapsed |= jockey3_process_out_packet(chip, buf + i * PLOYTEC_PKT_SIZE);
```

Touch points, from the study:

1. URB allocation and `usb_fill_bulk_urb()` lengths
   (`jockey3_init_playback_urbs()`, `jockey3_init_capture_urbs()`)
2. sub-packet loop in both completion handlers
3. `jockey3_init_out_packet()` / `jockey3_silence_out_packet()` per sub-packet
4. capture `urb->actual_length` -> sub-packet count, not a blind N
5. `period_bytes_min` -- period elapse is computed per sub-packet but can only
   be *reported* once per URB, so the honest minimum period is one URB span
6. **`jockey3_check_urb_stream_alive()`'s 1 ms window** (`jockey3.c:1540`,
   hardcoded `NSEC_PER_MSEC`). At N=8 and 44.1 kHz the URB span is 1.81 ms and
   the documented invariant -- "a healthy direction confirms itself within one
   packet interval" -- becomes false. **Rule: the liveness window must be at
   least one URB span.** The constant and its comment both change with N.
   This is the one most likely to produce a confusing false stall during
   bring-up.
7. `JOCKEY3_WATCHDOG_STALL_MS` (20 ms) still holds numerically -- 11 URB spans
   in the worst case -- but its justification text needs updating
8. ring depth: 8 URBs x 8 packets is 64 packets in flight, 14.5 ms at
   44.1 kHz, against 1.81 ms today. Re-choose it rather than inherit it;
   Ozzy's 4 is a reasonable starting point
9. `buffer_limits_validation.md` (workspace root, outside the repo)

Unaffected: the codec and its KUnit tests -- the codec still sees one packet
at a time.

### E2c -- measurement and regression

Against E1's baseline, at N = 1, 2, 4, 8 (N=1 must reproduce the baseline; if
it does not, the harness is wrong, not the driver).

**Preliminary results, N=1 vs N=2 at all four rates: `main`, see
`re/streaming_overhead.md`'s "E2c preliminary result" sections.**
`irq_per_s` halves almost exactly at every rate on both boards measured so
far, as predicted -- the completion-rate model is platform-independent.
`cpu_pct_sys_irq_soft` is where the platforms diverge: `x86_64-prod` shows a
15-19x idle-CPU drop at 88.2/96 kHz, `arm64-prod` only 1.4-3.2x at the same
rates (closer to the naive 2x model) -- the payoff is real on both but not
uniform, and should not be assumed to transfer from one host to another.
`armhf-prod` also shows a real reliability improvement: N=1's 88.2/96 kHz
completion rate was 2-3x the flat model (a recovery-feedback loop), N=2's
lands within ~9%, and across 4 runs there were zero full USB resets against
N=1's reliable stall-then-reset pattern. **Confirmed with the achievable-
latency sweep and MIDI OUT throughput check, both on `x86_64-prod`:**
minimum period doubles at N=2 (120/144 B -> 240/288 B) exactly as predicted;
MIDI OUT throughput holds at 2496-2497 B/s against a 2500 B/s target at all
four rates, confirming the per-sub-packet MIDI byte pull did its job. Still
open: full regression profile.

Beyond the E1 metrics, three things specific to this change:

- **xruns at small periods.** The minimum usable period grows with N. Sweep it
  (`JT-PCM-*`, `pcm_latency_sweep.py`) rather than assuming. **Confirmed:**
  see the study's "minimum period doubles" result above.
- **MIDI OUT jitter.** Throughput is unchanged -- every sub-packet still
  carries its own byte -- but a byte handed over just after an URB was filled
  waits up to one URB span. Expected to be inside the noise against the
  driver's own ~2500 bytes/s rate limit and the 320 us a MIDI 1.0 byte takes
  on a real wire, but measure it: `midi_leds.py` / `midi_controls.py` and a
  round-trip timing check.
- **Full regression.** `smoke`, then `functional`, then `regression` on
  `x86_64-prod` and `arm64-prod`. Rate-change behavior in particular
  (`JT-RATE-001`) -- the rate change tears down and restarts the URB ring, and
  that path is the one with history.

### Deliverable

- **Code (branch, then `main` if it wins):** the coalescing change with N
  chosen from the measurements, plus the constants above.
- **Documentation (main, either way):** measured before/after per platform per
  rate, the chosen N and why, the latency cost, and -- if it failed -- exactly
  how.

### Future direction (post-E2c): N derived from the requested period size

Frank's idea, worth recording before it's lost even though it's out of scope
for E2b/E2c: instead of a fixed compile-time N, derive it per PCM open from
the period size the application actually requested, so an application asking
for a large low-latency-tolerant buffer gets the full coalescing saving, and
one asking for a tiny period (real-time processing chasing minimum latency)
falls back to N=1 -- today's uncoalesced behavior -- automatically rather than
being handed a fixed URB span it did not ask for.

This fits the existing teardown/rebuild path used for rate changes
(`jockey3_pcm_hw_params()` already tears down and re-creates the URB ring),
so it is not a new mechanism, just a new input to the existing one. The open
design question is the mapping itself: `period_bytes_min` today is a
compile-time `N x subpacket_bytes`; with N derived instead of fixed, the
relationship inverts -- pick the largest N such that `N x subpacket_bytes <=`
the period size actually negotiated, floored at N=1 -- and that constraint
has to be enforced during `hw_params()` rather than declared statically in
`jockey3_pcm_hw_playback`/`jockey3_pcm_hw_capture`.

**Refinement (also Frank's): restrict N to powers of two.** With N a
compile-time constant, as in E2b, the compiler already folds every
`N x PLOYTEC_PKT_SIZE` and sub-packet-offset multiply into a shift, since
`PLOYTEC_PKT_SIZE` (512) is itself a power of two -- N's own value doesn't
matter for that today. It starts to matter the moment N becomes a runtime
value chosen per PCM open: the compiler can no longer fold a multiply by a
variable, so a power-of-two-only N (stored as a shift count, `1 << n_shift`)
keeps `JOCKEY3_*_XFER_SIZE` and the sub-packet loop's offset arithmetic
shift-only on every architecture, not just ones with a barrel shifter. The
cost is coarser granularity -- N can only be 1, 2, 4, 8, not 3 or 5 -- but
that costs nothing here: coalescing's payoff comes from fewer, larger URBs
regardless of exact period fit, since the device paces the wire itself
either way (study, Part 2) -- there is no reason to prefer landing on an
odd N over rounding down to the next power of two.

Deliberately not pursued now: it only pays for its added complexity if E2c's
N=1-vs-N=2 numbers show the saving is worth chasing per-application rather
than as one fixed driver-wide choice.

**Exit criterion:** either a merged change with numbers behind it, or a
written-up reason it cannot be done.

### Run ID cross-reference: which N a given hardware run used

`N` is a compile-time constant (`JOCKEY3_PLAYBACK_N`/`JOCKEY3_CAPTURE_N`,
`jockey3.c`); nothing in `run.json`/`result.json` records it, since the test
framework has no way to see a value that does not exist until compile time.
Frank has been noting it alongside each run id in his reports since the N=4
work started -- collected here as the authoritative index rather than left
scattered across chat history. Update this table (don't just append below
it) whenever a new run's N is known.

| N | Platform | Test(s) | Run ID(s) |
|---|---|---|---|
| 1 (baseline, pre-`dev/streaming-overhead`) | x86_64-prod | JT-PERF-001 | `20260825T154430Z-smoke`, `20260825T161559Z-smoke`, `20260825T170234Z-smoke` |
| 1 | arm64-prod | JT-PERF-001 | `20260825T154735Z-functional`, `20260825T161340Z-functional`, `20260825T170721Z-functional` |
| 1 | armhf-prod | JT-PERF-001 | `20260825T155503Z-smoke`, `20260825T161240Z-smoke` (canonical), `20260825T170435Z-smoke` (noisy, excluded -- post-96kHz restore-timeout contamination) |
| 2 (`3974582`/`854f7a8`) | x86_64-prod | JT-PERF-001, JT-PCM-007, JT-MIDI-004 | `20260825T195732Z-smoke`, `20260825T220718Z-functional`, `20260825T221542Z-functional` |
| 2 | arm64-prod | JT-PERF-001, JT-PCM-007, JT-MIDI-004 | `20260825T211430Z-functional`, `20260825T222215Z-functional`, `20260825T222515Z-functional` |
| 2 | armhf-prod | JT-PERF-001 (x4) | `20260825T212251Z-smoke` (clean), `20260825T213832Z-smoke` (2 self-recovered stalls), `20260825T214550Z-smoke` (clean), `20260825T215242Z-smoke` (clean) |
| 4 (`9317476`/`b6e5c86`) | arm64-prod | JT-MIDI-004, JT-PCM-007, JT-AUDIO-002, JT-RATE-001, JT-PERF-001 | `20260825T230043Z-functional`, `20260825T231128Z-functional`, `20260825T231624Z-functional`, `20260825T231814Z-functional`, `20260826T001324Z-functional` |
| 4 | x86_64-prod | JT-MIDI-004 (1 stall, escalated to reset), JT-PCM-007 (1 self-recovered stall), JT-AUDIO-002 (clean), JT-RATE-001 (4 stalls incl. 1 escalation), JT-PERF-001 | `20260825T233829Z-functional`, `20260825T234109Z-functional`, `20260825T234303Z-functional`, `20260825T234356Z-functional`, `20260826T001006Z-functional` |
| 8 | arm64-prod | JT-AUDIO-002 (clean), JT-RATE-001 (10 self-recovered stalls, 16.7%), JT-PERF-001, JT-PCM-007 | `20260826T002201Z-functional`, `20260826T002259Z-functional`, `20260826T003013Z-functional`, `20260826T003412Z-functional` |
| 8 | x86_64-prod | JT-PCM-007 (clean), JT-AUDIO-002 (clean), JT-PERF-001 (clean, but irq/s model breaks down -- see study), JT-RATE-001 (8 stalls, 3 escalated to full reset) | `20260826T003641Z-functional`, `20260826T003749Z-functional`, `20260826T003842Z-functional`, `20260826T004125Z-functional` |

Not yet run: a `JT-RATE-001`-scale (dozens of changes) comparison at N=1
and N=2, same boards as the N=4 runs above, to control for whether the
post-rate-change stall rate/pattern found at N=4 is N=4-specific or was
already present at lower N. N=8 planned next, both platforms.

---

## E3 -- The on-demand gate question, and only this question

**Question:** can the device resume bulk OUT and IN after a multi-second idle
gap with no EP0 traffic, within 20 ms?

The study recommends *against* on-demand streaming, so this experiment exists
only to close the question with evidence rather than inference. It is
independent of E1/E2 and can be skipped entirely without affecting them.

Nothing in the OpenVizsla corpus can answer this: neither vendor ever stops
streaming, and `capture_2026-08-13_macos_poweron_noapp` shows macOS streaming
141,524 packets with no application open at all.

### Method

With the device streaming, stop the URBs, wait 10 s, then restart, escalating
only as far as needed:

1. **Bare restart** -- resubmit the URBs, no EP0 traffic at all. Measure
   whether both directions resume and how long the first completion takes.
2. If that fails: **`ploytec_start_streaming()` only** -- `GET_STATUS` +
   `SET_STATUS` with the STREAMING bit. Two EP0 transfers, plausibly inside
   the budget. This is the branch in which on-demand is viable at all.
3. If that fails: **full `ploytec_initialize_device()`**. macOS cold init
   spans 103-142 ms including ~50 ms of fixed sleeps, and our own path carries
   a 3-5 ms `usleep_range()` plus four `usb_set_interface()` round trips --
   so reaching this rung means on-demand fails the 20 ms budget outright.

Also, and independently: **does MIDI IN still arrive on EP 0x83 while the PCM
URBs are stopped?** A controller whose knobs go dead when audio is closed is a
functional regression, not a tradeoff, and this is a DJ controller whose
primary job is MIDI. If the answer is no, on-demand is closed regardless of
how fast the restart is.

### The hook

This needs a temporary development-only trigger for stop/start -- a debugfs
file or a module parameter. Project policy permits a dev-time bench hook
provided it never ships; it stays on `dev/streaming-overhead` and is **never**
merged to `main`. The `dev/jockey3-watchdog-stall-injection` branch is the
precedent for how that has been handled before.

### Stop rules

- Rung 1 or 2 succeeds within 20 ms **and** MIDI IN survives -> on-demand is
  technically viable; re-open the cost/benefit against whatever E2 and E4
  delivered, remembering that with LED feedback riding byte 480 an idle timer
  would rarely fire at all.
- Anything else -> **stop.** Lever 3 is closed, and that is the answer.

### Deliverable

- **Documentation (main):** the answer, the timings, and the MIDI IN result,
  written into `re/streaming_overhead.md` Part 4 to replace the inference
  currently there.
- **Code:** none reaches `main`.

---

## E4 -- Idle rate downshift

**Question:** what does returning the device to 44.1 kHz after an idle period
actually save, and is it worth the added rate changes?

The original proposal, and the one already demonstrated by hand: on `pi1test`
a one-second 44.1 kHz `aplay` took interrupts from ~30,000/s to ~16,800/s and
system time from 50-60% to 10-20%, live.

**Only worth building after E1** quantifies what it saves on the platforms
that are actually targets -- pi1 is the motivating observation, not the goal.

### Design

A delayed work item, defaulting to a few seconds, taking `rate_mutex`,
cancelled by any PCM open. Not a timer callback -- it needs to sleep. The
delay should be a module parameter for the experiment; whether it stays one is
a separate decision.

### Open questions to settle, not assume

- **Gate on MIDI OUT activity too?** A rate change stops and restarts the URB
  ring, interrupting MIDI OUT for ~150 ms. If a controller app is driving LEDs
  with no audio open, a downshift would produce that gap out of nowhere.
  Probably harmless -- the same gap already happens on every `hw_params()` --
  but decide it deliberately.
- **The added rate changes.** The rate-change path measures 0 stalls in 486
  changes since `5505b28`, and 0 resets over 20,000 changes on `x86_64-prod`,
  but arm64 retains a small residual (12 playback / 6 capture stalls in 4,000,
  all recovered without a reset). A downshift timer *adds* rate changes to a
  system's lifetime. Quantify the added rate: how many downshifts does a
  realistic day of use produce?
- **First-open latency.** The next open at a non-44.1 kHz rate now pays a
  100-210 ms rate change that might previously have been free. Bounded and
  probably invisible, but measure it.

### Deliverable

- **Code (branch, then `main` if it wins):** the idle downshift work item.
- **Documentation (main, either way):** measured saving per platform, the
  added-rate-change cost, and the decisions on the questions above.

---

## Sequencing

```
E1 (measure)  --+--> E2a (gate) --> E2b (prototype) --> E2c (measure) --> merge?
                |
                +--> E4 (downshift)                                   --> merge?

E3 (independent, optional, likely terminal)
```

E1 is the only hard prerequisite. E2 and E4 are independent of each other and
compose in the final driver. E3 can run at any point, or not at all.
