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

### E2d (post-E2c): N derived from the requested period size

Now the active line of work, promoted from "future direction" once the
consolidated N=1/2/4/8 sweep (final row of the cross-reference table below)
showed coalescing is stable at every N and the per-N differences are small
enough that no single driver-wide N is obviously right for every workload.

#### The idea

Instead of a fixed compile-time N, derive it per PCM open from the period
size the application actually requested. An application that asks for a
large, latency-tolerant buffer gets the full coalescing saving; one that
asks for a tiny period (real-time processing chasing minimum latency) falls
back to N=1 -- today's uncoalesced behavior -- automatically, rather than
being handed a fixed URB span it did not ask for. In practice any period
large enough for ordinary desktop audio lands on the ceiling N=8; only a
deliberately small period (~16-32 frames) pulls a direction down.

#### Decisions settled (Frank, 2026-08-27)

1. **N is chosen per stream open, per direction**, as the largest power of
   two that fits, `1 <= N <= 8`. Playback and capture are separate
   substreams with independent `hw_params`; only the sample rate is shared
   (already pinned in `jockey3_pcm_open()` via
   `snd_pcm_hw_constraint_single`). So the two directions can and do settle
   on different N -- and their sub-packet sizes differ anyway (10 vs 8 PCM
   frames), so the same period lands them on different N.

2. **It is the period size that binds, not the buffer size.** The reason
   `period_bytes_min` scales with N today is that `period_elapsed` is OR-folded
   across the sub-packet loop in the completion handlers, so at most one
   period boundary may fall inside one URB. That is a per-period relation:
   `N x subpacket_bytes <= period_bytes`. Buffer size only enters via
   `periods_min = 2`. The derivation must therefore key off
   `params_period_bytes()`, not the buffer. ALSA permits a different period
   size per direction, which is consistent with decision 1.

3. **No new `hw_constraint` rule is needed.** The relation is `<=`, not a
   divisibility requirement, so the existing form -- a `period_bytes_min`
   that already equals `N x subpacket_bytes` -- is the right shape. Relax
   the static `jockey3_pcm_hw_playback`/`_capture` minimums to the N=1
   values (120 bytes playback, 144 capture) and let N adapt underneath
   them in `hw_params()`:

   `n_shift = min(3, ilog2(period_bytes / subpacket_bytes))`, guarded for
   the ratio being zero; use `ilog2()` rather than a hand-rolled loop.

   Sanity vectors (period frames -> N), directions genuinely diverging:

   | period frames | playback (10 f/subpkt) | capture (8 f/subpkt) |
   |---|---|---|
   | 16  | N=1 | N=2 |
   | 32  | N=2 | N=4 |
   | 64  | N=4 | N=8 |
   | 128 | N=8 | N=8 |

4. **Restrict N to powers of two, stored as a shift count.** With N a
   compile-time constant the compiler already folds `N x PLOYTEC_PKT_SIZE`
   and every sub-packet-offset multiply into a shift, because
   `PLOYTEC_PKT_SIZE` (512) is itself a power of two. That stops the moment
   N is a runtime variable: the compiler can no longer fold a multiply by a
   variable. Carrying N as `n_shift` (`N = 1 << n_shift`, `0 <= n_shift <= 3`)
   keeps `PLOYTEC_PKT_SIZE << n_shift` and `NSEC_PER_MSEC << n_shift` (the
   liveness window, `JOCKEY3_LIVENESS_WINDOW_NS`) shift-only on every
   architecture. Coarser granularity (1/2/4/8, never 3 or 5) costs nothing:
   coalescing's payoff is fewer, larger URBs regardless of exact period fit,
   since the device paces the wire itself (study, Part 2).

   **Carve-out:** keep a mirrored `u8 n_subpkts` next to `n_shift` and use
   *that* as the sub-packet loop bound. `for (sp = 0; sp < (1 << n_shift); sp++)`
   reads worse than `sp < n_subpkts` for no codegen gain. Both fields are
   set only while the ring is stopped, or under the stream spinlock.

5. **Allocate every URB for N=8 unconditionally** (8 x 512 B, ~32 KB per
   direction across the 8-URB ring) and vary only `transfer_buffer_length`.
   Reallocating transfer buffers on the restart path is added risk on the
   driver's most fragile code for the sake of ~28 KB.

#### The collision with the URB-lifetime model

URBs run free for the device's lifetime; playback in particular can never
stop, because MIDI OUT rides in byte 480 of every playback packet. Changing
N for a direction means tearing that direction's ring down and restarting it
-- the same destructive path as a rate change, which is the driver's
known-weakest area (Milestone 13; the capture-restart stall; the whole
watchdog/recovery ladder that the 2026-08-27 work went into hardening).

That makes the rule asymmetric in a way the plain statement hides:

- **Shrinking N is mandatory** -- a smaller period makes the running N
  illegal (a period boundary would be missed).
- **Growing N is purely an optimization** -- a bigger period does not break
  anything at the current N.

Taken literally, "largest N that fits" forces a teardown in the second case
too: a small-period application closes, a large-period one opens, and the
ring is rebuilt to go from N=2 to N=8 for a few percent of IRQ rate --
trading the fragile path against the optimization, in the wrong direction.

#### E2d-exp: the blocking experiment (does N have to tear the ring down?)

**Question:** does the firmware keep an already-running bulk ring streaming
if `transfer_buffer_length` changes between resubmissions of the same URBs?
E2a established `N x 512 B` only as a *static* property, chosen before the
ring starts; whether it can change mid-stream has never been asked.

- **If yes:** runtime N never tears the ring down. `hw_params()` sets a new
  `n_shift`, the ring turns over within at most 8 URBs (~14.5 ms worst case
  at 44.1 kHz, N=8), and `trigger START` lands after that. The
  shrink/grow-policy question dissolves -- always pick the optimal N.
- **If no:** shrink N eagerly (mandatory), grow N lazily -- take a larger N
  only when the ring is being rebuilt anyway (a rate change, or a direction
  with no active stream).

**Instrumentation (temporary, `dev/streaming-overhead` working tree, marked
`TEMPORARY EXPERIMENT -- do not merge`):** two live `0644` module
parameters `exp_pb_n` / `exp_cap_n`, default 8, each snapped to the nearest
power of two in `[1, 8]` by `jockey3_exp_xfer_len()`. They set
`transfer_buffer_length` at all three submit sites -- both completion-handler
resubmit paths and the `jockey3_start_urbs()` loop (so a rate change or
recovery re-arms at the current experimental N instead of snapping back to
8). The sub-packet fill and drain loops are deliberately left at the
compile-time N=8: the experiment isolates "does the wire tolerate a shorter
transfer on a running ring", not "is the audio bit-correct at that length".
Buffers are always allocated 8 x 512 wide, so shrinking the length only asks
USB core to move fewer bytes.

**Procedure:** build `build_module.sh <target> --uncommitted`; deploy with
`tests/hw/actions/reload_driver.sh`; start a full-duplex stream at the
default N=8 and let it settle; then flip `exp_pb_n` / `exp_cap_n` live
(8 -> 2 -> 4 -> 8, and playback-only vs capture-only) on the one continuous
stream. Watch `dmesg` for `EPROTO` / babble / `-71` resubmit failures and
the `watchdog_onset_*` / stall counters. Debug vs prod kernel does not
matter here -- the verdict is EPROTO/stall/onset, not audio quality.

Status: patch written and built (`x86_64-debug --uncommitted`, and
`build_jockey3.sh --gate build` clean bar the two known `-Wshadow` hits).
Hardware run deferred to 2026-08-28.

#### Observability requirement (agreed, do it regardless of the experiment)

Every N result to date was produced by editing the compile-time constant
and rebuilding, so the harness knew N by construction. Once N is derived it
does not -- and `aplay`/`arecord` coerce a period request the same way they
near-match a rate, so the harness cannot infer N from what it asked for
either. Without a read-back, `JT-PERF-001` / `JT-RATE-001` stop being
reproducible and the feature is unverifiable on hardware.

Add a permanent `dev_dbg` at `hw_params()` naming the chosen N per
direction. This is shipped diagnostics, not a test hook -- it does not
violate the driver's no-test-hooks rule. `JT-PCM-007`'s candidate ladder
can then be rebuilt around period sizes that land on each `n`, which also
closes its existing gap (its ladder never probes N=8's own tight
period-headroom boundary, only retests N=4's known-safe size).

#### Open question for Frank

E2c's regression gate says "N=1 must be byte-for-byte identical to the
pre-coalescing driver". Read as a code-shape claim, a runtime N cannot
satisfy it. Read as the comment actually words it ("every loop keyed off
these constants ... degenerates to the original single sub-packet code path
at N=1"), it is a behavior claim that `n_shift = 0` still meets. Confirm
which was meant before relying on it.

#### The `jockey3.c:157-168` comment

The `JOCKEY3_LIVENESS_WINDOW_NS` comment currently *predicts* this change
("would make this a shift rather than a multiply if N ever became a runtime
value"). When E2d lands, rewrite it as current-state rationale.

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

Once E2d lands, N stops being compile-time and this manual index is
superseded by the `hw_params()` `dev_dbg` read-back described in the E2d
section above; entries below this point are all fixed-N builds.

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
| 8 | x86_64-prod | JT-PCM-007 (clean), JT-AUDIO-002 (clean), JT-PERF-001 (clean, but irq/s model breaks down -- see study), JT-RATE-001 (8 stalls, 3 resets -- `resets_total_device` under-reported this as 0 due to a test-framework bug, see below), JT-RATE-003 (20,000-change overnight soak: 176 resets/0.88%, likewise under-reported as 0 -- see `re/rate_change_stall.md`'s 2026-08-26 follow-up) | `20260826T003641Z-functional`, `20260826T003749Z-functional`, `20260826T003842Z-functional`, `20260826T004125Z-functional`, `20260826T005005Z-functional` |
| 8 | armhf-prod | JT-PCM-007, JT-RATE-001, JT-PERF-001 (results pending) | `20260826T024815Z-functional`, `20260826T024405Z-functional`, `20260826T023733Z-functional` |
| 8 | x86_64-prod | JT-RATE-001, restricted to `96000<->48000` (`rate_stall_trace.bt` bpftrace script attached, see `re/bpftrace/`): 120 changes, 1 stall/1 reset (0.83%) | `20260826T142446Z-smoke` |
| 8 | x86_64-prod | JT-RATE-001, same restricted pair, **no bpftrace attached** (A/B non-perturbation check against the row above): 120 changes, 2 stalls/2 resets (1.67%), elapsed 542.4s vs. 541.5s with the tracer attached -- indistinguishable at this sample size, no measurable wall-clock cost. Non-perturbation not disproven, but n=120/2 events resolves nothing finer; see the caution beside the corrected N=4/N=8 figures above. | `20260826T143438Z-smoke` |
| 8 | x86_64-prod | JT-RATE-001, **redundant-restart fix applied** (`9b6f283`, manifest-verified build): 300 changes, 1 reset (0.33%), `watchdog_onset_total=21` (7%, 19 self-healed without a restart). Down from 2.5-3% per ~120 changes pre-fix, but the one reset that did happen shows a different, messier multi-context cascade (Capture direction, three overlapping recovery contexts) -- root-cause analysis and the deeper (not yet fixed) watchdog-fires-on-legitimate-silence finding in `re/rate_change_stall.md`'s 2026-08-26 follow-up. | `20260826T173215Z-smoke` |
| 8 | arm64-prod | JT-RATE-001, same fix, manifest-verified build: 300 changes, **0 resets**, `watchdog_onset_total=57` (19%, 56 needed a restart) -- consistent with arm64's established "stalls often, always self-heals" pattern; not a quiet run, just a reset-free one. | `20260826T173110Z-functional` |
| 8 | arm64-prod | JT-RATE-001, **rate-change liveness check now uses `JOCKEY3_WATCHDOG_STARTUP_GRACE_MS` (200ms) instead of a hardcoded 50ms** (`e1b14ff`, fixing the `playback_stall` finding below), manifest-verified build: 360 changes total across two runs, **zero `playback_stall`/reset/watchdog events of any kind** -- down from the 3.5% `playback_stall` rate the 20260827 `JT-RATE-003` soak measured pre-fix (see `re/rate_change_stall.md`'s 2026-08-27 follow-up). x86_64-prod re-run clean too, no regressions. Not a full soak, but 360 changes at a pre-fix 3.5% rate would have been expected to show ~12-13 events, so zero is a meaningful (if not yet conclusive) signal. | `20260827T145957Z-functional` (60), `20260827T150734Z-functional` (300) |
| 8 | x86_64-prod / arm64-prod | JT-PERF-001, same build as above. arm64 checked directly against the pre-fix N=8 arm64 baseline (`20260826T003013Z-functional`) per rate: `stream_44100` 0.30%->0.27%, `stream_48000` 0.40%->0.25%, `stream_88200` 0.44%->0.54%, `stream_96000` 0.40%->0.59% -- mixed, both directions, no consistent regression or improvement at this sample size (single run each side). x86_64 not compared line-for-line yet but reads in the same range as its earlier N=8 rows. | `20260827T150842Z-functional` (x86_64), `20260827T153251Z-functional` (arm64) |
| 1 | arm64-prod | JT-RATE-001, N=1 exposed a distinct false-positive class (2 near-instant completions per restart defeating the watchdog's startup classification, 29/200 changes) -- see `re/rate_change_stall.md`'s second 2026-08-27 follow-up. Superseded by the 8ドル row's manifest-verified build immediately below. | `20260827T162059Z-functional`, `20260827T180532Z-functional`, `20260827T192002Z-functional` |
| 1 | x86_64-prod | JT-RATE-001, same N=1 comparison, 3/240 false positives (much lower rate than arm64 at this N, same underlying mechanism) | `20260827T162105Z-functional` |
| 1/2/4/8 | x86_64-prod / arm64-prod | **Final consolidated sweep, all three startup-timeout fixes applied (`e1b14ff`/`0fd7982`/`0ec6b5b`, `JOCKEY3_STREAM_STARTUP_GRACE_MS` used everywhere), no bpftrace/trace_printk attached so timing is directly comparable across N.** `JT-PCM-007`/`JT-AUDIO-002`/`JT-RATE-001`/`JT-PERF-001` at every N, both platforms. `JT-RATE-001`: clean at N=1 (0/100 x86_64, 0/60 arm64), N=4 and N=8 (0/100 both platforms); N=2 arm64 clean (0/100), N=2 x86_64 had 1/100 -- `stalls_direction_first=1`, a full reset, isolated to the run's very first change after probe (the framework's own pre-existing "first change" bucket), not repeated anywhere else. Full `JT-PERF-001` comparison table, both platforms, all four N: `re/streaming_overhead.md`'s 2026-08-27 consolidated section. | N=1: `20260827T221951Z`/`20260827T222002Z` (PCM-007), `20260827T223513Z`/`20260827T223238Z` (AUDIO-002), `20260827T222703Z`/`20260827T222718Z` (RATE-001), `20260827T223803Z`/`20260827T223402Z` (PERF-001) -- N=2: `20260827T222343Z`/`20260827T222321Z` (PCM-007), `20260827T224535Z`/`20260827T224513Z` (AUDIO-002), `20260827T224632Z`/`20260827T224616Z` (RATE-001, x86_64 the one reset), `20260827T230119Z`/`20260827T230044Z` (PERF-001) -- N=4: `20260827T232010Z`/`20260827T232002Z` (PCM-007), `20260827T232139Z`/`20260827T232146Z` (AUDIO-002), `20260827T232228Z`/`20260827T232250Z` (RATE-001), `20260827T233145Z`/`20260827T233136Z` (PERF-001) -- N=8: `20260827T233533Z`/`20260827T233540Z` (PCM-007), `20260827T233729Z`/`20260827T233738Z` (AUDIO-002), `20260827T233809Z`/`20260827T233855Z` (RATE-001), `20260827T234741Z`/`20260827T234714Z` (PERF-001) -- x86_64-prod/arm64-prod respectively throughout. |

The N=1/N=2 `JT-RATE-001`-scale control comparison this table used to call
"not yet run" is done -- see the final consolidated row above. It answered
the question that blocked it: the post-rate-change stall rate/pattern
found at N=4/N=8 pre-fix was not N=4-or-above-specific, it was present
(worse, in fact) at N=1, and it was a driver bug, now fixed.

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
