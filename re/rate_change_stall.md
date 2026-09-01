# Capture URB stall after a rate change

> **RESOLVED 2026-08-17.** The cause was a control transfer this driver was
> not sending. `ploytec_start_streaming()` wrote the status byte back only
> when the STREAMING bit was clear, and after a rate change the device already
> reports it set -- so the write never happened, and the device's capture
> engine was never told to restart. Both vendor drivers issue it
> unconditionally, 115 times out of 115 in the corpus. Making it unconditional
> (`5505b28`) took `resets_per_change_pct` from **19.3% to 0.0%**: 0 stalls in
> 486 rate changes, against 168 in 870 before. Every downward pair that
> carried the fault -- 42.7%, 38.6%, 36.4% -- is now empty. See "The fix
> works" below.
>
> The 88200 arm was run too: **0 in 486 changes across both arms**, covering
> all four rates and all eight non-lateral pairs at n>=60 each. The two
> worst pairs on record, `88200->48000` (42.7%) and `96000->48000` (38.6%),
> are both empty.
>
> Still open: the **device wedge** under sustained resets (open question 3),
> which this run did not exercise because it performed no resets. The
> mitigation stack described below is now dormant rather than load-bearing,
> and whether any of it should be removed before submission is a separate
> question this document does not answer.
>
> **2026-08-21/22 follow-up: the mitigation stack was simplified, not
> removed.** `jockey3_recover_capture_stream()` -- the dedicated
> deferred-recovery function this document's "deferred recovery at the next
> capture open" line refers to below -- is deleted (commit `cd47c4c`,
> `implementation_plan.md` Milestone 13). Playback and capture recovery are
> now one shared function, `jockey3_recover_urb_stream()`, called from both
> `jockey3_pcm_hw_params()`'s post-rate-change check and
> `jockey3_pcm_prepare()`'s liveness check. The *behavior* this document
> measured did not change: a capture stall while idle is still left alone at
> rate-change time and still only recovered the next time a capture stream is
> opened -- deferred recovery is still exactly what happens. What changed is
> that there is no longer a dedicated function or remembered "capture owes a
> catch-up" state to implement that deferral; `jockey3_pcm_prepare()`'s own
> liveness check, now symmetric between directions, simply notices the
> still-stalled capture stream on open and calls the same function playback
> always used. The idle-capture gate itself (only reset capture immediately if a
> capture stream is actually open, to avoid an audible reset glitch on
> unrelated, working playback audio) is unchanged and was deliberately kept;
> see `notes.md`'s "Why the idle-capture gate exists, and why it's being
> kept" for the full rationale. Hardware-endurance-validated post-cleanup at
> 0 resets over 20,000 rate changes on x86_64-prod and 4,000 on arm64-prod
> (2026-08-22); a small residual stall rate on arm64 (12 playback / 6 capture
> stalls out of 4,000 changes) recovered every time without needing a device
> reset.
>
> **2026-08-26 follow-up: the long `JT-RATE-003` soak this document has been
> waiting on is in, at `N=8` -- and it exposed a test-framework bug that had
> been silently hiding every watchdog-triggered reset since the watchdog
> gained the ability to queue one (`e780ef4`, "make the URB liveness
> watchdog self-healing", 2026-08-23).**
>
> `cases/rate_change.py`'s `resets_total_device` only ever summed three
> named contexts -- `reset_on_rate_change`, `reset_after_urb_restart`,
> `reset_after_playback_prepare` -- matching the three call sites
> `jockey3_recover_urb_stream()` had when that counter was written. A
> fourth call site, `jockey3_watchdog_work()`'s own escalation (context
> `"watchdog"`, `jockey3.c:1744`), was never added to the list. Every reset
> it queued logged normally (`queuing full USB reset (watchdog)`, then
> `usb ...: reset high-speed USB device ...`) but was invisible to the
> metric and, via `branch_of()`, misclassified as a self-recovered
> `"deferred"` stall rather than a `"reset"`.
>
> `x86_64-prod`, capture arm, run `20260826T005005Z-functional` (~8.08 h,
> 20,000 changes) reported `resets_total_device=0`. The true count, counted
> directly from `dmesg.txt` (`queuing full USB reset (watchdog)` and the
> matching `usb ...: reset high-speed USB device` line, both bounded
> between this run's own `JT-MARK`s) is **176** -- exactly the 176 changes
> the run had already correctly counted as `stalls_per_change_pct=1.0%`
> (84 `down`/`cross`, 92 `up`/`within`): every one of those "self-recovered"
> stalls was actually a full device reset. That is `resets_per_change_pct
> = 0.88%`, not `0.0%`. This run therefore *does* exercise repeated resets
> and, contrary to what it originally appeared to say, is now the largest
> evidence to date on the **device wedge** question (question 3) -- it ran
> 176 resets back to back with no gap and never wedged.
>
> The same bug affects every `x86_64-prod`/`armhf-prod` `JT-RATE-00{1,3}`
> run taken between `e780ef4` (2026-08-23) and the fix (`reset_on_watchdog`
> added to `CONTEXT` and to `branch_of()`, same day as this note).
> `arm64-prod` is unaffected in the runs checked -- its watchdog resets were
> independently already known to be zero, and the raw dmesg count agrees.
> Recounted from raw `dmesg.txt` the same way: the N=4 `x86_64-prod`
> `JT-RATE-001` run (`20260825T234356Z-functional`, 100 changes) had 1 true
> reset (was reported as 0), and the N=8 100-change run
> (`20260826T004125Z-functional`) had 3 (was reported as 0) -- matching
> what had already been noted by hand in
> `re/streaming_overhead_experiments.md`'s N cross-reference table before
> the metric bug was found, which is how the discrepancy below was first
> spotted.
>
> **The "0.0% at N=8, 20,000 changes vs. 3/100 at N=8" conflict this
> section previously raised is not resolved, it is dissolved -- it was
> the counting bug, not a real disagreement.** The corrected figures (1%
> at N=4/n=100, 3% at N=8/n=100, 0.88% at N=8/n=20,000) do not by
> themselves establish or rule out a reset rate that climbs with N either:
> a 100-change sample cannot resolve better than roughly a factor of two
> either way (see "the number this is judged on" above), so 1% and 3% are
> not distinguishable from noise, and the only high-confidence figure is
> the 20,000-change 0.88%. There is still no same-build `N=1`/`N=2`
> `JT-RATE-001`-scale control run to compare it against -- that remains
> the blocking next step, now doubly so since the pre-08-23 comparisons
> this investigation leaned on cannot be assumed unaffected either without
> the same raw-dmesg recount.
>
> **2026-08-27 follow-up: overnight `JT-RATE-003` soak (20,000 changes, N=8)
> on `x86_64-prod` (`20260827T004510Z-functional`) and `arm64-prod`/pi4
> (`20260827T004800Z-functional`) found the hw_params rate-change liveness
> check's 50 ms window was itself too tight on arm64.** `x86_64-prod` was
> clean (1 reset, 2 playback stalls, 1 capture stall, 1 watchdog restart).
> `arm64-prod` had `resets_total_device=0` and no watchdog onsets at all, but
> `playback_stall_total=696` (3.5% of changes) -- every one of them
> `jockey3_pcm_hw_params()`'s own post-rate-change check
> (`playback_alive=0` in the `Rate change to N Hz left a stream stalled ...`
> line, `jockey3.c:2515-2518`) finding Playback not yet alive 50 ms after
> `jockey3_start_urbs()`, recovered by the light URB restart every time and
> never escalating. Spread evenly across the whole run and across all four
> target rates -- not a warm-up or rate-pair effect.
>
> This 50 ms window is the identical "first completion after a restart"
> latency that `JOCKEY3_STREAM_STARTUP_GRACE_MS` (200 ms) exists to give
> margin against (see that constant's comment, `jockey3.c:213-231`) -- but
> the hw_params check was left at a hardcoded `50` (not even the
> `JOCKEY3_PREPARE_CONFIRM_MS` constant it duplicated) when that grace period
> was introduced on 2026-08-26. 3.5% of rate changes needing the light retry
> at N=8 on arm64 means 50 ms had almost no margin there. Changed hw_params
> to use `JOCKEY3_STREAM_STARTUP_GRACE_MS` for this check instead, for
> consistency with the watchdog's own reasoning and to remove the magic
> number. Not yet re-validated on hardware; still no same-build `N=1`/`N=2`
> control run either (same open item as the 08-26 follow-up above).
>
> **2026-08-27 follow-up #2: N=1 exposed a "2-fast-completions-then-a-real-gap"
> pattern after every restart, not just after a rate change -- and it broke
> the watchdog's startup/steady-state classification in a way N=8 never did.**
> Investigating an N=1 vs. N=8 comparison run (`JT-RATE-001`, dozens of
> changes each, part of the N=1/2/4/8 control-run series this document's
> first 2026-08-27 follow-up called for): N=8 was clean, but arm64-prod at
> N=1 logged far *more* `watchdog_onset_steady_state` events than N=8 ever
> had (8.75% of changes in one 240-change run), the opposite of the naive
> expectation that a smaller N should leave more margin against
> `JOCKEY3_WATCHDOG_STALL_MS` (20 ms, fixed, not scaled with N). It does --
> worst-case URB span is 83-227us at N=1 (20 ms = ~88-240x that) vs.
> 0.67-1.81ms at N=8 (20 ms = ~11-30x) -- so the extra onsets are not a
> detection-margin artifact. Something real was different.
>
> Root-caused with a gated `TEMPORARY DEBUG` `trace_printk()` added to both
> `jockey3_playback_callback()` and `jockey3_capture_callback()`, logging
> `urb->status`/`jockey3_urb_check()`'s result/time-since-`urbs_started_time`
> for the first 5ms after every restart (`jockey3.c`, `dev/streaming-overhead`,
> not yet committed as of this writing). First hypothesis -- a stopped/error
> URB completion slipping through and falsely proving the stream alive --
> was refuted immediately: every early completion logged `status=0`,
> `urb_check=0` (`JOCKEY3_URB_OK`). They are genuine, successful completions.
>
> **The actual pattern, measured across all 201 restart groups in one
> 200-change run (`arm64-prod/20260827T192002Z-functional`)**: every single
> restart at N=1 gets **exactly 2** Playback completions landing ~55-170us
> after `jockey3_start_urbs()` -- almost certainly a small hardware-side FIFO
> on the device draining whatever was already queued -- followed by a real,
> highly variable gap before the next one:
>
> - 119/201 restarts (59%): no 3rd completion at all within 5ms of the restart.
> - The rest resume anywhere from ~150us (effectively continuous) up to
>   several ms later, with no obvious clustering -- a wide, continuous
>   distribution, not a bimodal "fine" vs. "broken" split.
> - 29/200 rate changes in that run (14.5%) happened to have that gap exceed
>   the fixed 20 ms `JOCKEY3_WATCHDOG_STALL_MS` and got logged as a stall.
>   The other 171 are silent, sub-threshold instances of the exact same
>   underlying event -- the flagged onset rate is only the visible tail of a
>   distribution that is present on effectively every restart, not a
>   distinct failure mode affecting 14.5% of changes.
>
> A completion just means the USB endpoint ACKed the OUT transfer -- it does
> not mean the device's DAC/clock pipeline has caught up with the new rate.
> Working hypothesis, *not wire-verified*: at N=1 each URB is tiny (one
> Ploytec sub-packet, tens of bytes), small enough that the first couple can
> fit into a small hardware FIFO on the device and get ACKed immediately even
> while the device is still mid-resync internally; once that FIFO is full the
> device stops ACKing until it is genuinely ready, which is where the
> already-established ~20-23ms restart latency (the same figure
> `JOCKEY3_STREAM_STARTUP_GRACE_MS` was sized against on 2026-08-26) becomes
> visible as a real gap. At N=8 each URB carries 8x the data, so it is much
> less likely two whole N=8-sized transfers fit through the same FIFO before
> the gate closes -- `last_callback_time` correctly stays untouched for the
> entire resync window at N=8, which is consistent with N=8 never showing
> this. Ruled out as an alternative explanation: a driver-side cleanup gap
> after `usb_kill_anchored_urbs()` letting a stale pre-stop completion leak
> through -- `jockey3_stop_urbs()` (`jockey3.c:1385-1442`) is synchronous
> (`usb_kill_urb()`/`usb_kill_anchored_urbs()` block until every completion
> handler has actually run) and explicitly zeroes `last_callback_time`/
> `urbs_started_time` *after* that, before `jockey3_start_urbs()` stamps a
> fresh `urbs_started_time` and submits 8 new URBs -- there is no window for
> a leftover completion from before the stop to taint the new cycle.
>
> **The driver defect this exposed** (distinct from, and not fixed by, the
> 50ms->200ms hw_params change in the first 2026-08-27 follow-up above):
> `jockey3_watchdog_check()`'s `startup` classification
> (`jockey3.c:1747-1752` as of that first follow-up) was binary on "has
> `last_callback_time` been set at least once since the restart" -- so the
> first of those 2 fast completions flipped it from the wide 200 ms
> `JOCKEY3_STREAM_STARTUP_GRACE_MS` to the tight 20 ms
> `JOCKEY3_WATCHDOG_STALL_MS` immediately, while the device was still
> genuinely inside its restart-settling window. One completion is not proof
> of steady flow, only proof one buffered transfer drained.
>
> **Fix (Frank's call, not yet hardware-validated)**: made `startup`
> purely time-based -- `now - urbs_started_time < JOCKEY3_STREAM_STARTUP_GRACE_MS`,
> regardless of how many early completions have landed -- instead of keyed
> off `last_callback_time == 0`, in both `jockey3_watchdog_check()` and its
> scheduling companion `jockey3_watchdog_next_delay_ms()` (which had the same
> stale-classification shape for computing the next poll deadline, though not
> itself a correctness bug since the check function re-derives state fresh
> every tick). Deliberately no "N consecutive completions" requirement --
> the fixed grace window alone is expected to be sufficient. Both
> `JOCKEY3_STREAM_STARTUP_GRACE_MS`'s own comment and
> `jockey3_watchdog_check()`'s kernel-doc updated to describe the new
> semantics.
>
> **Confirmed on the wire, same day: this is a hardware/firmware property,
> not a Linux-driver artifact.** Frank's observation that the FIFO-drain
> hypothesis should be checkable directly, since it claims to be
> hardware-only -- checked two already-existing OpenVizsla captures rather
> than taking a new one, since both already carry rate changes:
>
> - `capture_2026-08-17_linux_ratechange.txt` (this driver, predates all
>   N=8/coalescing work by over a week): rate change to 44.1kHz, right after
>   the burst's last EP0 write --
>   ```
>   14.351730  OUT: 34.5 (playback)
>   14.351741  OUT: 34.5   <- 11us later, second completion
>   14.356900  OUT: 34.5   <- 5,159us later -- the real gap
>   ```
>   then steady ~210-250us cadence resumes.
> - `capture_2026-08-17_macos_ratechange.txt` (vendor CoreAudio driver,
>   Reloop/Ploytec 3.3.17, entirely different USB stack): same shape at a
>   different rate change --
>   ```
>   22.105829  OUT: 8.5 (playback)
>   22.105839  OUT: 8.5   <- 10us later, second completion
>   22.106633  OUT: 8.5   <- 794us later -- the gap
>   ```
>   then steady ~200-230us cadence.
>
> Both captures show exactly 2 near-back-to-back OUT completions right after
> the rate-programming burst ends, then a gap larger than steady-state
> cadence before regular flow resumes -- on a driver-side capture that
> predates this investigation's driver changes entirely, and independently
> on the vendor's own driver and USB stack. The gap magnitude differs (5.16ms
> vs. 794us) but sits within the same wide, variable distribution the
> driver-side `trace_printk` data already showed (150us to several ms across
> 201 restarts) -- not two different phenomena. What remains open is only
> the *count* (exactly 2, not 1 or 3) and *why 2 specifically* -- the small
> hardware-FIFO explanation is still the working theory for that detail, not
> confirmed by these two captures, but the pattern itself (fast pair, then a
> gap, on the wire, independent of driver) no longer needs to be inferred.
>
> Also open: whether this same 2-fast-then-gap pattern exists at N=2/N=4
> (would support it being about available buffer depth scaling with N x
> `JOCKEY3_N_URBS`) or is N=1-specific for some other reason -- part of the
> same N=1/2/4/8 control-run series still in progress.
>
> **2026-08-27 follow-up #3: the one "substream open" reset in the fix's own
> validation run was a real, isolated stall -- tipped into an unnecessary
> full reset by missing a confirm deadline by half a millisecond.**
> `arm64-prod/20260827T195958Z-functional`'s single remaining event
> (`resets_total_device=1` out of 200 N=1 changes, tagged `substream open`
> not `idle`, so a different class from the false positive #2 above fixed)
> was cross-checked between `wdcheck_trace.log` (the driver's own
> `trace_printk`) and `re/bpftrace/rate_stall_trace.bt`'s independently
> tracked completions, both agreeing on the sequence (dmesg-clock times):
>
> | time | event |
> |---|---|
> | 629771.248518 | watchdog catches a genuine 23ms gap in Playback completions, 786ms after the last restart (unrelated to any rate change) -- confirmed by bpftrace's own completion tracker independently agreeing `age=23ms` |
> | 629771.248560 | light URB restart triggered |
> | 629771.252098 | `jockey3_start_urbs()` returns |
> | 629771.300475 | the restart's own confirm poll (then `JOCKEY3_PREPARE_CONFIRM_MS`, 50ms) gives up, escalates to a full USB reset |
> | 629771.300948 | the real first completion after the restart lands -- **473 microseconds later** |
>
> The light restart had actually worked; the 50ms confirm window just
> wasn't quite enough margin to see it happen. Same problem class as the
> two fixes above (a real, variable N=1 restart latency vs. a too-tight
> fixed window), now found at a third call site.
>
> **Clock-offset gotcha hit again doing this analysis, worth recording
> precisely**: `re/bpftrace/rate_stall_trace.bt`'s own `nsecs` timestamps do
> NOT reliably match dmesg's printk clock either, contrary to what earlier
> follow-ups in this document assumed. In this run bpftrace's log was
> offset from dmesg by the same ~7.16s constant `wdcheck_trace.log` carried
> (i.e. bpftrace and `trace_printk` share one clock that differs from
> dmesg's) -- a *previous* run happened to have a near-zero bpftrace-to-
> dmesg offset, which is what created the impression bpftrace always
> matches dmesg. `correlate_trace.py window` assumes zero offset and would
> have silently shown the wrong window here. Always derive the offset
> per-run from a known pair (a `wdcheck_trace.log` `age_ms`-nonzero line
> against its matching dmesg onset line) and apply it to bpftrace
> timestamps too, not only `trace_printk` ones.
>
> **Consolidation, Frank's call**: all of this driver's "has this direction
> reached steady streaming yet" checks are really the same question asked
> from different call sites, and every one of them had turned out too
> tight at some point in this investigation -- a hardcoded 50ms in
> `jockey3_pcm_hw_params()` (follow-up #1, fixed), the completion-gated
> `startup` classification in the watchdog (follow-up #2, fixed), and now
> `JOCKEY3_PREPARE_CONFIRM_MS` (also 50ms, shared by
> `jockey3_recover_urb_stream()`'s post-restart re-confirm and
> `jockey3_pcm_prepare()`'s own liveness check). Rather than re-tune each
> one separately, `JOCKEY3_PREPARE_CONFIRM_MS` is retired and every one of
> its call sites now uses `JOCKEY3_STREAM_STARTUP_GRACE_MS` (200ms), the
> same value the watchdog itself uses -- one time budget for the whole
> "restart to steady flow" question, used everywhere it is asked
> (`jockey3.c`, `dev/streaming-overhead`). Frank's reasoning: a too-tight
> budget can cause an escalation (reset) that a wider one would have
> avoided, as this very event demonstrates, while a too-loose one only
> delays detecting a device that is genuinely not coming back -- and that
> fault stays wedged far longer than any reasonable extra margin would
> ever paper over, so there is no real cost to erring wide. Not yet
> hardware-validated.
>
> The rest of this document is the investigation as it ran, kept because the
> reasoning is the record of how the cause was found -- and because several
> of its intermediate conclusions were wrong in instructive ways.

The problem as it stood: changing the sample rate leaves the capture endpoint
(`0x86`) not delivering, and the driver carries a stack of mitigations for it
-- stream liveness polling, a watchdog, a direction-aware corrective reset,
and deferred recovery at the next capture open.

This document records what is actually measured. It exists because the
mitigations were built before the fault was characterized, and the
measurements below do not all support the assumptions they were built on.

The companion document `usb/init_timing_comparison.md` covers the *cold-boot*
fault, which is fixed and is a different mechanism.

## The fault, stated precisely

After a rate change the capture stream stops completing URBs. The driver
notices in one of three places:

- `jockey3_pcm_hw_params()` waits 50 ms per direction and logs
  `Capture URB has stalled.`
- with a capture stream **open**, it escalates to a full `usb_queue_reset_device()`
- with **no** capture stream open, it logs and defers recovery to the next
  capture open, to avoid an audible reset glitch on healthy playback

Playback is unaffected throughout -- audible on speakers, and confirmed by the
playback rate ratio being correct on every change.

## What the vendor traces say: this is not a settling problem

Measured by `usb/rate_change_stream_timing.py`, relative to `SET_STATUS`:

| | n | sequence span | EP 0x05 resumes | EP 0x86 resumes |
|---|---|---|---|---|
| macOS | 47 | 103-142 ms | **0-1 ms** | **1-21 ms** |
| this driver (power-on traces) | 4 | 166-211 ms | 70-76 ms | 70-85 ms |

macOS resumes playback within a millisecond of `SET_STATUS` and the device
produces capture data again within 21 ms, in all 47 sequences across four
captures. **The vendor leaves no settling window after reconfiguration**, so
the device does not need time to reconfigure -- which rules out the mechanism
that turned out to explain the cold-boot fault. A power cycle restarts the
firmware; a rate change does not.

## What was ruled out on hardware

**The 50 ms wait is not too short.** Our own traces showed capture returning at
70-85 ms, later than the wait, which suggested the driver was declaring a stall
for a stream about to return on its own. Raising the wait to 250 ms in
`jockey3_pcm_hw_params()` changed nothing: `stalls_capture` stayed at 19 of 20
changes. The only thing that moved was `urb_stall_restarted_ms`, from
1433-1683 ms to 1640-1878 ms -- the extra wait before escalating. **Capture
genuinely does not return within 250 ms.** The hypothesis is dead; the
mitigations are load-bearing.

**The watchdog's onset figure is not independent evidence.**
`urb_stall_onset_ms` reads ~1000-1030 ms, but that is the watchdog's polling
interval, not a measured onset -- it reports the age at the first poll after
the stall. It cannot resolve anything faster than 1 s.

## The measurements, 2026-08-14

All on `x86_64-prod`, build `c64d7744` unless noted, 20 rate changes per run.

| run | capture stream | s/rate | stalls | resets | capture data |
|---|---|---|---|---|---|
| 194246 | none (playback only) | 1 | **19/20** | 0 | not checked |
| 194739 | none, 250 ms wait experiment | 1 | **19/20** | 0 | not checked |
| 195536 | attempted, `arecord` broken | 1 | **19/20** | 0 | none delivered |
| 204834 | open and working | 4 | **9/20** | 9 | **live on 20/20** |

The last row is the first valid capture measurement this case has ever
produced. Everything before it was either playback-only or defeated by a test
bug (`arecord -d 1.0`, rejected as an invalid duration).

Two things follow. With a capture stream open the fault is **less frequent**
(9 of 20 rather than 19 of 20) and **always recovers**: every stall was
followed by a reset, and capture returned 83-85% non-zero frames on every
change. Without a capture stream open, the stall happens nearly every time and
nothing recovers it -- by design, since recovery is deferred.

**This comparison is confounded and must not be quoted as a result.** Two
variables changed between rows 3 and 4: the capture stream being open (deferred
path vs reset path) *and* the duration per rate (1 s vs 4 s).

Worse than confounded: **the two arms do not measure the same quantity**, and
no experiment design can make them. Three things, all readable in `jockey3.c`,
were established on 2026-08-15 before spending any more hardware time.

### The playback-only arm counts one outage, not nineteen stalls

`jockey3_recover_capture_stream()` is reached only from `jockey3_pcm_prepare()`,
on a capture open. In a run that never opens capture, nothing ever recovers a
deferred stall -- so after the first one the capture stream stays dead for the
rest of the run, and `jockey3_wait_urb_stream_started()` re-logs `Capture URB
has stalled.` at *every subsequent change* because the stream never came back.

19 of 20 is therefore one stall re-detected nineteen times. It is not a
per-change incidence and cannot be compared against the 9 of 20 from a run
where each change starts from a stream a reset had restored. Only the
capture-open arm measures incidence at all.

### The stall message is emitted from four call sites

`jockey3_wait_urb_stream_started()` logs one identical string from
`hw_params()`, from `prepare()`, and twice inside
`jockey3_recover_capture_stream()` (after the lightweight URB restart, and
after the full reset). A single change that escalated all the way can
legitimately produce four of them. Any figure derived by counting occurrences
of that string -- including `stalls_capture` in `lib/rules.yaml` -- counts
neither stalls nor changes.

Only the one from `hw_params()` measures the rate change itself. The case now
resolves each occurrence to its call site by the context line that follows it,
and reports that one as `capture_stall_hw_params`.

### Which branch runs was decided by a race nobody was watching

The rate change happens inside the *first* `hw_params()` of the pair; the second
finds the rate already set and returns. So whether `capture_open` is 1 or 0 --
reset on the spot, or defer to the next capture open -- was decided by whether
`aplay` or `arecord` got there first. Both were spawned back to back, so a run
nominally testing the reset path could have spent an unknown fraction of its
changes on the deferred one, and neither the run record nor the log said which.

### What the experiment should be instead

Hold `seconds_per_rate` at 4 (below that the timing checks are not enforced and
the measurement length changes with the variable), and vary one thing at a time:

```sh
cd tests/hw
./runner.py --case JT-RATE-001 --unattended                       # baseline: reset branch
./runner.py --case JT-RATE-001 --unattended \
    --param rate_change_stream=playback                           # deferred branch
./runner.py --case JT-RATE-001 --unattended --param gap_seconds=3 # recovery time
```

`gap_seconds` idles between changes with nothing open, which is the right
instrument for "the device needs time between changes" -- `seconds_per_rate` is
not, because it moves the measurement window at the same time. There is a
concrete mechanism for a gap to matter: the reset `hw_params()` queues via
`usb_queue_reset_device()` is **not** waited for there (only
`jockey3_recover_capture_stream()` waits), so at zero gap change N+1 can begin
while change N's reset is still in flight. Consecutive changes are not
independent events.

The playback-only arm is still worth one run, but as a question of its own --
*does a deferred stall ever clear by itself?* -- not as a third point on the
same axis.

## What is instrumented now

`JT-RATE-001` was, until 2026-08-14, a playback-only test wearing a general
name: it never opened capture, so it never checked capture data and never
exercised the reset path -- the path that carries the risk. It now:

- plays and records simultaneously on every change
- classifies capture four ways: `live`, `nodata` (no frames -- the transport
  never restarted), `silent` (frames, all zero -- the converter never
  restarted), `error`
- measures the **effective** sample rate from the clock: playback elapsed
  against expected, and capture frames over the wall-clock time they took to
  arrive. Not frames over requested duration, which measures nothing, since
  `arecord -d` derives its frame target from the rate it asked for
- writes a `JT-MARK` per change and attributes every stall and reset to the
  change that caused it, reported on screen as the run proceeds

and, as of 2026-08-15:

- **fixes which branch is under test.** `rate_change_stream: capture |
  playback | race` orders the two opens and waits for the leading one to pass
  `hw_params` before starting the other, so `capture_open` is what the run says
  it is. `branch_change_*` records what each change actually did, so a change
  that drifted onto the other branch is visible rather than assumed away
- **separates the four call sites** that share the stall message, so
  `capture_stall_hw_params` is the incidence figure and
  `capture_stall_on_open` / `_after_urb_restart` / `_after_reset` are the
  recovery attempts
- **closes each change's log window when its streams close**, with a second
  marker. A reset completing after the streams are shut now lands in a
  "between changes" bucket instead of being charged to the change that runs
  next -- which, given that the reset is not waited for, is where a good deal
  of it was going
- **groups incidence by direction, by rate pair, and by clock family**
  (44k1 vs 48k), which is question 2 below, computed rather than eyeballed
- **adds `gap_seconds`**, idle time between changes with nothing open
- **measures the rate from the device's own clock instead of from a
  stopwatch on a process.** See below.

Arms are selected with `--param` rather than by editing `catalog.yaml`, and
`run.json` records the resolved parameters, so every run identifies its own
configuration.

### Measuring the rate from hw_ptr rather than from a stopwatch

The old effective-rate figure timed a whole `aplay` or `arecord` invocation:
frames delivered over wall clock from `Popen` to exit. That charges process
start-up, device open and every buffer in the path to the sample rate. The
result is

```
observed = rate / (1 + startup / duration)
```

which reads 10-17% low at four seconds even when the device is perfect. It is a
fixed additive offset, not a scaling error, so the only lever it offers is a
longer run: 5% needs ~10 s per rate, 1% needs ~50 s. That is where the
"observed rate deviates from the real rate" puzzle came from.

Shrinking the ALSA buffer is the wrong lever and was dropped rather than
deprioritized. It can only recover its own fill time -- 4096 frames at 48 kHz
is 85 ms against a start-up cost measured in hundreds -- and on a
`SNDRV_PCM_INFO_BATCH` device, where the pointer advances only once per
completed URB, a small buffer invites xruns that would corrupt the very
measurement.

What replaced it costs nothing and beats the 5% target by an order of
magnitude. `/proc/asound/cardN/pcmXc/sub0/status` carries `hw_ptr`, the total
frames the **hardware** has moved, and reading that file calls
`snd_pcm_update_hw_ptr()` while the stream is running, so the value is fresh as
of the read. Sample it every 20 ms alongside `CLOCK_MONOTONIC` and the slope is
the device's clock, with start-up, open and all buffering outside the window
rather than inside it. Buffer size does not enter at all: a buffer shifts
latency, not rate. No custom ALSA client is needed, and none is worth writing
for this question.

Two things are cut out of the window rather than averaged into it:

- **the settling time** -- 500 ms after the pointer first moves, per the
  estimate that prompted this
- **plateaus** -- stretches where the pointer stopped. This one is not an
  accuracy refinement but a correctness requirement: a 1.5 s stall inside a 4 s
  run drags the average down 37%, so a stall would be reported as a wrong
  clock, in the same bucket as a genuine rate error, on precisely the changes
  this case exists to study.

A backwards step is treated as a break, not a stall: `hw_ptr` restarts at zero
after a device reset, which on this driver is routine.

Three things fall out of it for free:

- **`startup_s_*`** -- `Popen` to the first frame the hardware moved. The
  excluded cost, measured rather than apportioned. This settles the
  startup-versus-buffering question with data.
- **stall onset and duration from the host**, at the 20 ms poll interval
  instead of the watchdog's one second, per change, with no dependence on the
  kernel log at all. For milestone 13 this may be worth more than the rate
  number.
- **a working `xruns` metric.** The old one sampled `alsa.xruns()` before and
  after the loop, both times with nothing open -- the status file reads
  `closed`, `pcm_status()` returns `{}`, and the difference of two absent
  values is zero. It has reported a clean run on every run ever recorded. The
  watchers sample during the stream, which fixes it, and it matters here
  because an xrun inside a measurement window invalidates that window's rate.

The tolerance is set at 5%, not at what the instrument can do. The question is
whether a rate change took effect; at much tighter bounds it would start
reporting the device's crystal.

One consequence worth noting: at 5% the 44100 <-> 48000 step (8.1%) is
resolvable, where at the old 20% it was not. The default sweep no longer has a
blind transition.

### Two things to check before believing the first run

- **`lead_stream_configure_timeouts` must be 0.** The ordering that
  `rate_change_stream` promises is achieved by waiting for the leading stream's
  `/proc/asound` status to leave `OPEN`. If that state string is not what this
  driver reports, every wait runs to its 5 s timeout, the ordering silently
  does not happen, and the branch labels are fiction. The metric says so; the
  stub in `selftest.py` cannot, because it is hardware-dependent by nature.
- **`branch_reset` is a precedence label, not a clean count.** On the capture
  arm, `hw_params` queues its reset without waiting, and the `prepare` that
  follows can find capture still dead and start the URB-restart ladder on top
  of a reset already in flight. Such a change is labelled `reset`. Read
  `escalated_total` and `prepare_capture_total` beside it.
- **Polling the status file runs `snd_pcm_update_hw_ptr()` more often than the
  URB completion handler alone would.** On a stalled capture that is harmless,
  since `avail` stays at zero. But if `arecord` starts failing *differently*
  now that the watchers exist, that is the cause and not a driver change.
  Read `pointer_quantum_max` from the first run rather than deriving it: it is
  the measurement's resolution floor, and at a few hundred frames over a
  window of seconds it should be well under 1% (0.23% at 512-frame
  granularity over 3.5 s, computed).
- **`pointer_plateau_changes_*` should be near zero on healthy changes.** The
  static run at the end of every trace -- the process has stopped, the
  substream has not closed yet -- is excluded from the window but deliberately
  not counted as a plateau, and is reported as `pointer_tail_hold_s_*`
  instead. If the plateau count comes back equal to the number of changes, that
  exclusion is not working and the count means nothing.
- **`xruns` is summed over two windows of different lengths.** The playback
  watcher stops when `aplay` exits, which on the capture arm is while `arecord`
  is still running. Read the per-direction `xruns_*` metrics, not the total.

The distinction between `nodata` and `silent` matters for where it sends an
investigation: no samples is the URB stream not restarting, all-zero samples
would be the converter not running. They were conflated at first, producing a
verdict that blamed the audio engine while playback was audibly fine.

### One thing that is not instrumented, and should be

**The reset duration is no longer observable from the log.** `lib/rules.yaml`
looks for `waited N ms for reset completion`, `selftest.py` has a fixture for
it, and `catalog.yaml` listed a `reset_wait_ms_histogram` metric. The driver
does not emit it. It once did: `775b70e` ("Make the usb_reset asynchronous")
added

```c
dev_dbg(&chip->intf0->dev, "%s waited %d ms for reset completion.\n", ...);
```

and `c8fff65` ("prepare for batching") dropped it. What is left in
`jockey3_wait_for_reset_completion()` is a `dev_dbg` on entry and a `dev_warn`
only on timeout, so the metric has been empty since. Checked against all five
trees (`alsa-jockey3`, `~/sound`, `~/sound-build`), which agree.

The catalog entry no longer claims the metric. Restoring the line would cost
one `dev_dbg` and would answer directly whether the ~334 ms in the code comment
still holds under the sweep -- worth doing, but **not in the same change as the
instrument rewrite**, or the first comparison is read against a driver that
moved underneath it.

## The number this is judged on

**`resets_per_change_pct` — how often a rate change costs a USB device reset.
It must trend to zero.** `JT-RATE-001` prints it at the end of every run and
`ledger.py` trends it per target; `JT-RATE-003` reports the same figure so a
short run and a ten-hour one stay comparable.

**Baseline, 2026-08-15, `x86_64-prod`, `rate_change_stream=capture`:**

| run | changes | stalls | resets | `resets_per_change_pct` |
|---|---|---|---|---|
| first | 20 | 6 | 6 | 30.0% |
| second | 20 | 9 | 9 | 45.0% |

Two runs of the same build and the same arm, 30% and 45%. The fault is
probabilistic and twenty changes is a small sample, so **a single run cannot
show an improvement of less than roughly a factor of two.** Quote the metric
with its change count, and use `iterations_per_run` (or `JT-RATE-003`) before
concluding that a driver change moved it.

What is stable across both runs is the shape, not the rate: every stall was on
a downward, family-crossing transition, and every one resolved.

**The arm is part of the number and must be quoted with it.** On the capture
arm every capture stall reaches `hw_params()` with `capture_open=1` and resets
on the spot, so stalls and resets are the same event by construction. On the
playback arm the same stall is deferred and met by a URB restart first, which
may well clear it — so that arm can report a much lower percentage with the
driver completely unchanged. Compare like with like, or a change of arm will
read as a fix.

A reset is an audible interruption to whatever was playing, so both places the
driver queues one count towards it: the on-the-spot reset in
`jockey3_pcm_hw_params()`, and the escalation in
`jockey3_recover_capture_stream()` after a URB restart failed. A URB restart
that *worked* is not counted — it is the outcome to want more of, and it is
reported beside the percentage so that "same stalls, handled more cheaply"
does not look identical to no progress at all.

That companion figure, `urb_restarts_that_avoided_a_reset`, is **structurally
zero on the capture arm** and says nothing there:
`jockey3_recover_capture_stream()` is only reached from `prepare()` on a
capture open, which the capture arm never takes. Zero means "not on this path",
not "the cheap path does not work". The playback arm is where it carries
information — one more reason to run it.

It is deliberately not part of the pass criterion. Any threshold above zero
would bless the fault, and a threshold at zero would fail every run until the
fault is gone. It is a trend line, not a gate.

It is computed from the run totals rather than from the per-change
attribution, and that is load-bearing: on this very run the markers failed and
every per-change figure read zero while the totals stayed correct. The one
number the driver is judged on must not be able to go quietly wrong.

## The 2026-08-15 run: the stall follows the downward transitions

First run on the rebuilt instrument. `x86_64-prod`, kernel
`7.2.0-rc5-alsa-prod+`, 20 changes, capture arm, 4 s per rate, no gap. Verdict
PASS: capture live on all 20, both clocks correct.

**The clock measurement works.** Worst error 0.10% on both directions against
the 5% bound, and the start-up it excludes measured 0.23-0.64 s on capture and
0.02-0.29 s on playback -- i.e. the old whole-invocation figure was carrying
6-16% of bias at four seconds, exactly as predicted. No plateaus, no xruns.

**Six of twenty changes stalled and reset**, and the on-screen output said
`0/20`. Both figures came from the same run because the per-change markers
never reached the kernel log: the labels contained `@`, which is outside the
charset `priv/jockey3-testctl` validates against, so every *start* marker was
rejected while every *end* marker (`#gapN`, no `@`) got through. With only end
markers, each window ran from one change's end to the next change's end -- so
every event was charged to the preceding change's gap, and the per-change table
read zero. `lib/kmsg.py` now sanitizes labels against the helper's own charset,
`selftest.py` checks the two against each other, and the case suppresses the
whole per-change table rather than printing zeros when markers are missing.

Reading the windows back with the one-change shift undone, the six stalls fall
on changes 4, 6, 10, 12, 16 and 20:

| transition | direction | family | stalled |
|---|---|---|---|
| 88200 -> 48000 | down | cross | **4/5** |
| 96000 -> 44100 | down | cross | **2/5** |
| 44100 -> 88200 | up | within | 0/5 |
| 48000 -> 96000 | up | within | 0/4 |

**Every stall is on a downward transition; no upward transition stalled.**
6/10 down against 0/9 up. That is the long-standing suspicion in
`implementation_plan.md`, measured for the first time.

It is one run and it does not separate two hypotheses. In this sweep both
downward steps cross between the 44k1 and 48k clock families and both upward
steps stay within one, so **direction and family crossing are perfectly
correlated** and a result that follows one follows the other. The earlier claim
in this document that the default sweep was balanced for the family question
was wrong. Breaking it needs a downward step within a family or an upward step
across one:

```sh
./runner.py --case JT-RATE-001 --unattended \
    --param sweep_order=as-given --param rates=[96000,48000,96000,44100]
```

which gives 96000->48000 down-within, 48000->96000 up-within, 96000->44100
down-cross and 44100->96000 up-cross. If the stall follows the downward steps
regardless of family, it is direction. If it follows the crossings, the clock
source is being reprogrammed and that is a different investigation.

Also worth noting from this run: all six stalls took the reset branch and all
six recovered -- `escalated` and `unrecovered` were zero, and capture came back
live on every change. The mitigations are working; what is unexplained is why
the fault is one-directional.

## 2026-08-16: the schematic explains what to rule out, not what to blame

Before more hardware time was spent, the service manual was read for the
clocking topology. The device has two independent clock domains fanned into
the Ploytec USB part through a 74HC00 NAND pair (oscillator select) and a
74HC74 flip-flop (divide-ratio select):

```
DSP oscillator   24.576  MHz  --> /512 = 48000 Hz   or  /256 = 96000 Hz
XTAL oscillator  22.5792 MHz  --> /512 = 44100 Hz   or  /256 = 88200 Hz
```

So "crosses 44k1/48k family" and "switches the NAND oscillator mux" are the
same event in hardware, and every downward step in the original interleaved
sweep (`88200->48000`, `96000->44100`) happens to do both at once, alongside
switching the flip-flop's divide ratio from /256 to /512. Three candidate
mechanisms -- oscillator mux glitch, divider resync, and "downward" as a pure
host-side artifact -- were confounded in one bit each. This motivated the
`sweep_order=as-given`, `rates=[96000,48000,96000,44100]` run proposed in the
previous session, which puts one down step within a clock family
(`96000->48000`, oscillator unchanged) and one down step across it
(`96000->44100`, oscillator changed), and the mirror image upward.

## 2026-08-16: divider direction confirmed, oscillator crossing ruled out

Four hardware runs, all `x86_64-prod`, build unchanged across the session:

| run | config | down stalls | up stalls |
|---|---|---|---|
| `20260816T204542Z-functional` | interleave, `[44100,48000,88200,96000]`, 25 iter | 23/50 | 0/49 |
| `20260816T210602Z-smoke` | as-given, `[96000,48000,96000,44100]`, 25 iter | 25/50 | 0/49 |
| `20260816T223625Z-smoke` | as-given, `[96000,48000,96000,44100]`, **60 iter** | 62/120 | 0/119 |
| `20260816T231358Z-smoke` | interleave, `[44100,48000,88200,96000]`, 25 iter (repeat) | 22/50 | 0/49 |

Direction alone predicts every stall across all four runs -- 132/220 down,
**0/366 pooled up** -- which was already the 08-15 finding. What the as-given
runs add is the split by whether the oscillator mux also switched:

| pair | oscillator | divider | n=25 (`210602`) | n=60 (`223625`) |
|---|---|---|---|---|
| `96000->48000` | unchanged (DSP) | /256->/512 | 15/25 (60%) | 33/60 (55%) |
| `96000->44100` | DSP->XTAL | /256->/512 | 10/25 (40%) | 29/60 (48%) |
| `48000->96000` | unchanged (DSP) | /512->/256 | 0/25 | 0/60 |
| `44100->96000` | XTAL->DSP | /512->/256 | 0/24 | 0/59 |

At n=25 the within-family cell looked worse than the cross-family one (60%
against 40%), which read like the oscillator switch was *helping*. At n=60
that gap collapsed to 55% against 48% -- well inside the ~6.5pp standard
error for n=60 at p~0.5, i.e. noise. **The 96000->48000 cell never touches
the NAND oscillator mux at all** (both rates run off the same 24.576 MHz
DSP oscillator, only the flip-flop's divide ratio changes) and it stalls at
least as often as the cell that does switch oscillators. That rules out an
oscillator-mux glitch as the mechanism: the fault tracks the flip-flop's
divide-ratio transition (`/256 -> /512`, i.e. toward the lower rate in
either family), not the NAND gate's source select. "Family crossing" was a
correlate of the real variable, not the variable itself.

One thing this cannot separate, because the hardware doesn't offer the
combination: every within-family step on this device is exactly 2:1
(44100<->88200, 48000<->96000), so "the divide ratio changes" and "the ratio
is not a power of two" stay tied together here. If a future finding turns on
this, it cannot be resolved from this device alone.

## 2026-08-16: the within-run drift did not reproduce

The 08-15 run showed the downward-stall rate climbing from 0% to 44% across
one 100-change run, bucketed by quartile. Two more full-length runs came back
flat, bucketing every change (not just downward ones) by quartile of the run:

| run | Q1 | Q2 | Q3 | Q4 |
|---|---|---|---|---|
| `20260816T204542Z-functional` (the original climbing run) | 2/25 | 5/25 | 5/25 | 11/25 |
| `20260816T210602Z-smoke` | 6/25 | 7/25 | 6/25 | 6/25 |
| `20260816T223625Z-smoke` | 15/60 | 19/60 | 14/60 | 14/60 |
| `20260816T231358Z-smoke` (repeat of the original config) | 5/25 | 7/25 | 4/25 | 6/25 |

Only the original run shows a trend; the other three, including a same-config
repeat of it, do not. Treat the drift as an unreplicated one-off, not a
mechanism to build an explanation around. It is not ruled out -- one run each
way is a coin flip's worth of evidence -- but it no longer belongs on the
priority list above the direction finding.

## 2026-08-16: a device wedge under sustained resets, distinct from the everyday stall

`20260816T213343Z-smoke` (as-given, 60 iterations, `gap_seconds=0`) did not
just fail statistically -- it wedged the device. Reconstructed from
`dmesg.txt`, change-by-change:

- Changes 1-100 ran the ordinary pattern: some fraction of downward changes
  stall at `hw_params()`, `usb_queue_reset_device()` fires, capture comes
  back. Ordinary so far.
- From roughly change 70 onward the reset frequency climbed -- by change 90
  through 100, nearly every downward change was resetting (changes 70, 74,
  76, 78, 80, 82, 84, 86, 88, 90, 92, 96, 98 all reset in that stretch).
- Immediately after change 100, all three endpoints started returning `-71`
  (`EPROTO`) simultaneously: `Playback URB error`, `Capture URB error`,
  `MIDI IN URB error`, each counting consecutive failures, each hitting
  `stopped after 8 consecutive URB errors` within about 100 ms of the first
  one. This is not the capture-only stall the mitigations target -- it took
  down playback and MIDI too, which is otherwise unaffected throughout every
  other run in the corpus.
- The device dropped off the bus (`USB disconnect, device number 125`) and
  re-enumerated -- but the *same* `-71` cascade recurred immediately on the
  freshly-enumerated device, twice more (device numbers 126, then 127),
  three full disconnect/re-enumeration cycles in about 30 seconds before the
  firmware finally responded normally again (device number 2).
- After that recovery, rate-change reliability was markedly worse than
  baseline for an extended stretch: a second internal attempt sequence
  (visible in the log as `change1` recurring with a fresh device) shows a
  reset on very nearly every change, well above the 44-55% baseline
  established above.

`usb_queue_reset_device()` -- a USB port reset -- did not clear this; only a
full physical disconnect/re-enumeration did, and it took three tries. This
looks like sustained back-to-back stalls pushing the device into a state the
driver's existing recovery path cannot reach, which is a materially different
(and more serious) failure than the one this document otherwise
characterizes. `gap_seconds=0` means the reset `hw_params()` queues is never
waited for before the next change can start (noted already in the 08-15
"what should be instrumented" section) -- consecutive unwaited resets
overlapping is the most concrete mechanism on hand for why a sustained run
would degrade the device rather than just repeatedly recovering it.

The 240-change target was not reached because of this; a rerun of the same
config (`20260816T223625Z-smoke`) completed cleanly and did not wedge. One
wedge in five full-length runs across the session -- not frequent, but not
explained, and it is the more consequential open question of the two now
that direction/family is resolved. It belongs with the existing open
findings on device wedging (see project memory), not folded into the
capture-stall mitigations that already handle the ordinary case.

## 2026-08-17: the vendor sequence is direction-blind, and three concrete divergences

The priority-1 captures asked for below were taken (`capture_2026-08-17_*`,
macOS / Windows / Linux, one trace each, sidecars written). They close the
`down`/`within` gap outright: macOS contributes 11 `96000->48000` and 9
`48000->96000`, Windows 11 and 11. Together with the pre-existing corpus this
is 144 vendor rate changes -- 114 new, against the 30 the corpus held before
-- with every one of the six (direction x divider_class) cells populated on
both platforms.

**The tool of record is `usb/rate_burst_profile.py`, new here.** It replaces
the EVENT block with the *burst* -- a maximal run of `SET_RATE` transfers --
as the unit of analysis. That is not a refinement, it is a correctness fix:
`classify_rate_transitions.py` keeps only the last `GET_RATE(ep=none)` and the
last `SET_RATE` per EVENT, which is exact for the vendor traces (one change
per event, always) but silently wrong for ours. The Linux trace's event 5
holds three rate changes and a full re-enumeration; the classifier reduced it
to one mis-paired transition, and reported 10 transitions where the trace
contains 21. Any conclusion drawn from `classify_rate_transitions.py` about a
*driver* trace should be re-derived.

### The answer to the direction question: no

Across 136 bursts the sequence shape is a function of the platform and of
nothing else. Per platform it is one shape, and the same shape for every
direction and every divider class:

| platform | n | writes | endpoint order | quiet before | split | quiet after | terminator |
|---|---|---|---|---|---|---|---|
| macOS | 58/59 | 6 | `86,86,05,86,05,86` | 50.3-51.4 ms | ~11 ms after write 1 | 50.2-51.3 ms | `SET_STATUS 0x32` |
| Windows | 56/56 | 7 | `86,05,86,05,86,05,86` | 50.4-52.6 ms | ~5-9 ms after write 2 | none (0.0-0.4 ms) | `SET_STATUS 0x32` |
| Linux | 18/21 | 6 | `86,86,05,86,05,86` | 50.4-51.2 ms | ~11 ms after write 1 | 50.2-51.1 ms | `GET_STATUS` (second read) |

The single macOS outlier is the cold-init burst (5 writes, no leading lone
write); the three Linux outliers are the post-reset reprogram bursts
discussed below. Nothing partitions by direction -- not burst length, not
endpoint order, not the width or placement of either quiet window. This is
the outcome step 3 of the plan pre-registered as "also a result": **the
vendor drivers use an identical EP0 sequence going down and going up**, so
no direction-dependent wait or extra poll can be justified by reference to
them, and the investigation moves off `ploytec_proto.c`'s EP0 sequence.

Two notes on reading that table. The quiet windows are placed differently on
the two platforms -- macOS waits ~50 ms *after* the `GET_RATE(ep=none)` probe,
Windows *before* the `GET_STATUS`/`GET_RATE` probe pair -- so the metric that
makes them comparable is the gap entering the whole probe-and-program block,
which is what `pre_block_gap_ms` measures. And the two platforms differ in
where the burst splits: macOS writes one lone `0x86`, waits, then the 5-write
`86,05,86,05,86` run; Windows writes the `86,05` pair, waits, then the same
5-write run. The 5-write tail is identical on both.

### Divergence 1: our driver never sends SET_STATUS (the substantive one)

Every one of the 115 vendor bursts terminates with `SET_STATUS wValue=0x0032`.
None of ours do -- all 21 terminate with a second `GET_STATUS` instead.

The cause is in `ploytec_start_streaming()` (`ploytec_proto.c:206`), which
writes the status byte back only when the `PLOYTEC_STATUS_STREAMING` bit
(0x20) is clear. After a rate change the device reports 0x32, so the bit is
already set, the condition is false, and the write is skipped. The vendors
issue it unconditionally -- their own preceding `GET_STATUS` also returns
0x32, and they write 0x32 back anyway. The conditional is an inference this
driver made about what the write is *for*; the traces show the vendors do not
share it.

This is worth stating carefully. Skipping `SET_STATUS` is demonstrably **not
sufficient** to cause a stall: our driver omits it on every change and most
changes still resume. The hypothesis is that the write is what restarts the
device's *capture* engine specifically -- consistent with the fault being
capture-only, and with the wire evidence in the next section -- but it is a
hypothesis, and the write's actual effect on firmware that already has the
bit set cannot be read off a trace.

`ploytec_start_streaming()` is **not** `ploytec_set_rate()`, so acting on this
does not touch the EP0 rate sequence at all.

**Implemented 2026-08-17.** The condition is gone; the status byte is written
back with `PLOYTEC_STATUS_STREAMING` set on every call. The trailing
`ploytec_get_status()` went with it -- the vendors do not read the status back
after the write, and its result was discarded. Our terminator is now
`GET_STATUS`, `SET_STATUS 0x32`, byte-identical to both vendors.

**Validated on hardware 2026-08-17 -- the stall is gone.** See the section
below.

### Divergence 2: Windows clears endpoint halt *after* the change, not only before

**Deliberately not implemented.** The two vendors disagree here -- macOS
clears halt only in the preamble, as we do, and Windows only afterwards -- so
there is no vendor consensus to move toward, and changing it would align us
with one platform by diverging from the other. It stays on the list as the
next thing to try if the `SET_STATUS` change does not move
`resets_per_change_pct`.

43 of Windows' 56 rate changes end with `CLEAR_FEATURE(ENDPOINT_HALT)` on
`0x86` and then `0x05`, roughly 246 ms after `SET_STATUS` and immediately
before streaming resumes -- which is what the 248-509 ms `resume_in_ms` /
`resume_out_ms` figures for Windows in `rate_change_stream_timing.py` are
measuring. Our driver, and macOS, clear halt only in the preamble, before the
rate is programmed. The split does not partition by direction or divider class
(it runs about 75/25 in all six cells), so it does not bear on the direction
question, but it is a second concrete ordering difference and a far cheaper
recovery to try than `usb_reset_device()`.

### Divergence 3: macOS resumes capture in 1-21 ms; we take ~55 ms, or never

`rate_change_stream_timing.py` measures, from `SET_STATUS`, when the device
resumes producing on `0x86`: macOS 1-21 ms on all 59 changes, every direction.
Windows 248-509 ms, but that is host policy -- it is the post-change
`CLEAR_FEATURE` above gating it, not the device. Our driver's figures are
absent from that table entirely, because the script anchors on `SET_STATUS`
and we never send one; `rate_burst_profile.py --resume` measures the same two
quantities anchored on the end of the burst instead, and covers driver traces.

The Linux figures split cleanly in two, with nothing in between:

| | n | EP 0x05 (playback) | EP 0x86 (capture) |
|---|---|---|---|
| changes that succeeded | 18 | 50.7-51.7 ms | 52.3-71.0 ms |
| changes that stalled | 3 | 51.5-51.6 ms | **410.0-433.8 ms** |

Playback is indistinguishable between the two rows -- it always resumes at
~51 ms, which is the driver's own 50 ms wait. Capture either follows it within
about 20 ms or does not come back until the reset completes; the 410-434 ms
figures are measuring the reset, not a slow device. The gap between 71 ms and
410 ms is empty across all 21 changes.

The two tools cross-check. Anchored the same way (end of burst), macOS reads
52.0-73.0 ms on EP 0x86 across all six cells -- subtract its ~51 ms settle
window and that is the 1-21 ms `rate_change_stream_timing.py` reports from
`SET_STATUS`, which is an independent derivation agreeing to the millisecond.

It also sharpens what is actually wrong. **On the changes that work, we are
already as fast as macOS** -- 52.3-71.0 ms against 52.0-73.0 ms, the same
number. There is no general slowness to fix and no settling window to widen.
The entire defect is the 3 changes in 18 where capture does not start at all,
which is consistent with a discrete missed trigger rather than a race being
lost by a margin.

### What the wire shows during a failing rate change -- open question 4, answered

This did not need the dedicated capture the question reserved for it. The
Linux trace contains three stall-and-recover episodes already (events 2, 5 and
7, each a post-reset 5-write reprogram burst following a downward
divider-crossing change). Taking event 5 -- `88200->48000`, `down`/`cross` --
and reading the bulk traffic directly out of the parsed trace:

- The burst completes normally and the `GET_RATE(ep=0x86)` verify read returns
  the new rate. Nothing on EP0 reports a fault.
- Playback OUT on `0x05` resumes ~50 ms later at full rate (~48 packets per
  10 ms), runs for about 60 ms, then stops -- that is our own teardown for the
  queued reset, not the device quitting.
- **Capture IN on `0x86` produces nothing at all** between the rate write and
  the re-enumeration -- not one packet. It returns at full rate (~60 packets
  per 10 ms) in the first bin after the reset completes, 410 ms after the
  burst, and only then.

For contrast, on the successful change in event 3 (`96000->44100`, also
`down`/`cross`) capture IN resumes in the same 10 ms bin as playback OUT. All
three stall episodes (events 2, 5 and 7) behave alike, and the per-endpoint
resume table above separates them from the 18 successful changes with no
overlap. So the failure mode is not slow recovery or a partial stream:
capture starts within ~20 ms of playback, or it does not start until something
resets the device.

One caveat on the negative result -- `parse_openvizsla.py` discards NAKs, so
"no packets" means no data moved; it does not distinguish a silent endpoint
from one NAKing every poll. That would need a re-parse retaining NAKs, and it
is the one question the existing traces cannot answer.

### How often each side resets the device

`SET_ADDRESS` counts, read off the traces: macOS 2, of which one is the
initial enumeration -- so **1 mid-session reset in 58 rate changes**. Windows
1, the initial enumeration -- **0 in 56**. Linux 3, none of them initial (the
device was already enumerated when the capture started; event 1 holds no
`SET_ADDRESS`), and each falls in one of the three stall episodes -- so **3 in
18**, or one change in six.

All three were checked against the enclosing event rather than assumed: the
Windows one sits at t=0.235 ms in event 1, macOS's first at t=0.000 in event
1, and none of the Linux three is in event 1 at all.

That ratio is the quantitative form of the whole problem. Both this driver and
macOS re-enumerate the device occasionally after a downward divider-crossing
change; at 3/18 against 1/58 we do it roughly **ten times more often**.

### Open question 5 was measuring the parser, not the driver

The 166-211 ms figure our sequence has been charged with since 08-15 is an
artifact of where `extract_events.py` anchors an event. The first transfer of
each of our rate-change events is a `GET_FIRMWARE` whose reported `dur(us)`
is enormous -- 86,809 us on event 3, 117,235 us on event 4 -- against 22-180
us for every other transfer in the same block. `span` is measured from that
transfer's `t=0.000`, so the idle time before the sequence started is counted
as part of the sequence.

Subtract it, and the five clean single-change events (3, 4, 8, 9 and 11 --
one rate change, no reset) read **121.6, 122.6, 122.1, 123.6 and 121.9 ms**.
macOS's ordinary rate changes are 124-126 ms. We are not slower than the
vendor; if anything we are marginally faster.

This is not our artifact to fix, and it is not platform-specific: Windows
shows the same inflated leading transfer routinely (`SET_CONFIGURATION`
42-75 ms) and macOS shows it on event 23 (`SET_INTERFACE`, 116,005 us). It
appears on the first EP0 transfer after a long run of streaming traffic, on
every platform. Worth a note in `usb/README.md` so the next person does not
re-derive a 40 ms discrepancy that is not there; `span` should not be quoted
for an event whose first transfer carries a multi-millisecond duration.

### The fix works: 0 stalls in 486 rate changes

`20260817T161831Z-smoke`, `x86_64-prod`, module build-id `09c3e409` from
`5505b28`, `JT-RATE-001` with `sweep_order=as-given`,
`rates=[96000,48000,96000,44100]`, `seconds_per_rate=4`, `gap_seconds=0`,
`iterations_per_run=61` -- 244 changes, 61 per cell, which clears the n>=60
bar step 4 set.

Two arms, `20260817T161831Z-smoke` (`rates=[96000,48000,96000,44100]`) and
`20260817T164625Z-smoke` (`rates=[88200,48000,88200,44100]`), both
`sweep_order=as-given`, `seconds_per_rate=4`, `gap_seconds=0`,
`iterations_per_run=61`, module build-id `09c3e409` from `5505b28`. Between
them they cover all four rates and all eight non-lateral pairs at n>=60 each.
Before figures are every trustworthy pre-change run, any sweep order:

| pair | class | before | after |
|---|---|---|---|
| `88200->48000` | down/cross | 32/75 = **42.7%** | **0/61** |
| `96000->48000` | down/within | 56/145 = **38.6%** | **0/61** |
| `96000->44100` | down/cross | 80/220 = **36.4%** | **0/61** |
| `88200->44100` | down/within | *no baseline* | **0/61** |
| `48000->96000` | up/within | 0/213 | 0/61 |
| `44100->88200` | up/within | 0/75 | 0/60 |
| `44100->96000` | up/cross | 0/142 | 0/60 |
| `48000->88200` | up/cross | *no baseline* | 0/61 |
| **total** | | **168/870 = 19.3%** | **0/486** |

`resets_per_change_pct` went 19.3 -> **0.0**. P(0 stalls in 486) under the old
rate is 5e-46. The rule of three puts a 95% upper bound of **0.62%** on
whatever the residual rate now is -- so this is not "less frequent", it is
below what 486 changes can detect.

Every downward pair that ever carried the fault is now empty, including the
two worst on record. The upward pairs were already clean and stayed clean, so
nothing was traded away. `88200->44100` and `48000->88200` had no historical
baseline -- no earlier sweep produced them -- so they are newly covered rather
than newly fixed.

Not covered by either arm: the two **lateral** pairs, `44100<->48000` and
`88200<->96000`, where the divide ratio does not change. The hypothesis
predicts they never stalled in the first place and no sweep has ever produced
one, so this is a gap in the record rather than a risk.

**Verification, because a zero is exactly what a broken instrument reports.**
This document has been burned once by markers failing silently while every
per-change figure read zero. Both arms were checked the same way, and both
pass identically:

- `attribution_trustworthy` is `True` and `rate_check_blind_steps` is 0.
- Each run's own `JT-MARK` markers bound a ~1090 s window matching its
  `duration_s`, containing 489 markers for 244 changes. Filtered to that
  window, the kernel log contains **no** `Capture URB has stalled.`, no
  `Resetting device to recover`, no `reset high-speed USB device`, no URB
  errors and no submit failures.
- **`dmesg.txt` in these two runs is an ~18 hour ring buffer**, so it holds
  many earlier runs. The first arm's file contains 62 stall lines and the
  second's 10, every one of them outside its own run's window. Do not count
  them without filtering to the marker window first -- read raw, either file
  argues the opposite of what the run measured. *Fixed for future runs:* the
  runner now writes a run-start marker and trims `dmesg.txt` to it
  (`kmsg.run_log()`), falling back to the whole buffer with a loud header if
  the marker never lands. These two files predate that and still need manual
  filtering.
- Capture was genuinely exercised, not merely quiet: 244 measurements per
  arm, `capture_frames_ratio` min **1.0** (before: min 0.0, i.e. changes that
  delivered nothing), `capture_rate_ratio_min` **0.902** and **0.901**
  (before: 0.0), `steady_ratio_capture` 1.000 throughout.
- The loaded module is build-id `09c3e409...`, `dirty: false`, git `5505b28`
  -- the commit under test -- in both arms.

A note on one metric that looks alarming and is not: `capture_effective_hz`
reads 6-9% low here. That is the gross whole-invocation figure, which is low
by construction (`rate / (1 + startup/duration)`); the steady-state hw_ptr
measurement, `steady_ratio_capture`, is 1.000. Both are unchanged from before.
Likewise `capture_nonzero` sits at 0.83-0.85 in both, which is a pre-existing
property of the test signal, not a regression.

### What this does and does not settle

It settles that the unconditional `SET_STATUS` write eliminates the capture
stall, and therefore that the write is what restarts the device's capture
engine -- the hypothesis divergence 1 raised. The direction/divide-ratio
finding is not overturned by this, it is explained by it: the downward
divide-ratio transitions were the ones where the firmware needed the restart
trigger it was never being sent.

It does not settle the **device wedge** under sustained resets (open question
3). That fault was only ever reachable through repeated resets, and this run
performed none, so the wedge was not exercised rather than fixed. It stays
open.

The longer `JT-RATE-003` is now in, at `N=8` on `x86_64-prod` -- see the
2026-08-26 follow-up in the banner above (0 resets in 20,000 changes). A
lateral-only sweep (`rates=[44100,48000]` / `[88200,96000]`,
`sweep_order=as-given`) to put a number on the one class no sweep has ever
produced.

One thing that *is* already covered: the other call site of
`ploytec_start_streaming()` is the probe path (`jockey3.c:1919`), and
`20260817T161758Z-smoke` ran `JT-PROBE-001` three times on the same module
(build-id `09c3e409`), all passing. At cold init the device reports status
`0x12`, so the old conditional wrote too and behavior there is unchanged --
that was an inference from the traces, and this run makes it a measurement.

### Still missing

The Linux trace has **no `down`/`within` transition** -- its sweep gives 9
`down`/`cross`, 8 `up`/`within`, 1 `up`/`cross`. The stall was characterized
on hardware at `96000->48000`, but the wire evidence above is from
`down`/`cross`. Both cross the /256->/512 divider transition, so the episodes
are very likely the same fault, and that is an assumption, not a measurement.
A Linux capture of `96000<->48000` cycled until it stalls would settle it; it
is added to "Captures needed" below.

## 2026-08-23: an upward change fails outright at EP0, post-fix

`x86_64-prod`, bench build (watchdog self-healing + the concurrent-recovery
fix, not yet released). During `JT-AUDIO-002`, the 44.1->48kHz transition --
an **upward** change, contradicting the 08-15 finding above that every
observed stall fell on a downward one -- failed outright at EP0:
`Firmware version read failed: -110` (`ETIMEDOUT`), `Rate change to 48000
failed: -110`, repeated three times over about 4 seconds.

The watchdog independently noticed Playback had stopped completing at the
same moment (same underlying cause), tried its light restart, that did not
hold, and escalated to a full USB reset -- which itself ran long enough that
the driver's own 1000 ms wait bound gave up (`Timeout waiting for reset
completion`) while the real reset kept running via the USB core's own
workqueue and completed on its own after the driver had already moved on.

The device fully recovered: 88.2kHz and 96kHz worked cleanly in the same run,
and the next run passed outright. No concurrent-recovery collision this time
(contrast the bug fixed by "ALSA: jockey3: prevent concurrent recovery
ladders from racing each other") -- this looks like the same
already-tracked hardware/firmware-timing instability as the rest of this
document, just caught by the watchdog directly rather than only by
`hw_params()`'s post-rate-change check, and with an occasionally slower
reset than the driver's internal timeout expects. Whether an upward EP0
failure is a genuinely separate mode from the downward capture-stall this
document otherwise tracks, or the same instability surfacing a different
way once the divide-ratio fix closed off the common path, is unresolved.

## 2026-08-26: the watchdog fires on a rate change's own deliberate silence

Follow-on from the 2026-08-26 counting-bug follow-up above, using
`re/bpftrace/rate_stall_trace.bt` (kprobes on `jockey3_stop_urbs()`,
`ploytec_set_rate()`, `jockey3_start_urbs()`, `jockey3_watchdog_check()`,
`jockey3_recover_urb_stream()`, and the playback/capture completion
callbacks). N=8, `dev/streaming-overhead`, `x86_64-prod` unless noted.

### The mechanism: two independent recovery paths share no coordination

`jockey3_pcm_hw_params()` holds `rate_mutex` across its whole
stop/set-rate/start sequence (confirmed reading `jockey3.c:2298`).
`jockey3_recover_urb_stream()` -- the watchdog's own recovery call --
needs the same mutex for its stop+start pair, so the two cannot corrupt
shared state concurrently. But `jockey3_recover_urb_stream()`'s liveness
check (`jockey3_check_urb_stream_alive()`) ran only once, *before* trying
for the mutex. Traced twice on hardware (`20260826T152050Z-smoke`,
`20260826T154222Z-smoke`): `hw_params()`'s own restart succeeds, then a
watchdog call that had been blocked on `rate_mutex` acquires it moments
later (2.9ms in one case) and repeats a full stop+start on a stream that
had *just* recovered -- sometimes with a third such cycle from a
different call site layered on top (`"opening a capture stream"`,
`jockey3_pcm_prepare()`'s own check). The redundant cycle is the one that
misses its 50ms liveness budget (`JOCKEY3_PREPARE_CONFIRM_MS`) and
escalates to a full USB reset that the original rate change never needed.

**Fixed** (`dev/streaming-overhead` `9b6f283`): `jockey3_recover_urb_stream()`
re-checks `jockey3_check_urb_stream_alive()` immediately after entering
`scoped_guard(mutex, &chip->rate_mutex)`, before repeating the stop+start.
A call that raced a legitimate recovery is now a no-op.

### Why the watchdog fires at all: it is not measuring a real fault

The deeper question -- did the stream actually stall, or is the watchdog
firing on its own deliberate silence -- resolves cleanly by reconstructing
the *real* completion timeline from the trace's own captured completions,
independent of the driver's `last_callback_time`/`urbs_started_time`
bookkeeping:

| run | real last completion -> onset gap | `STOP_URBS` -> onset gap | dmesg's own reported gap |
|---|---|---|---|
| `20260826T152050Z-smoke` | 225.6 ms | 225.2 ms | "20 ms" |
| `20260826T154222Z-smoke` | 116.8 ms | 116.4 ms | "23 ms" |

**Caveat added 2026-08-26, after the fact:** the "real" column above mixes
clocks -- `STOP_URBS`/completion timestamps came from bpftrace's `nsecs`,
the onset gap from `dmesg`'s own timestamp, and a later, cleaner
investigation (see "Ground truth via trace_printk" below) found `dmesg`'s
clock and `ktime_get_mono_fast_ns()`-family clocks (bpftrace's `nsecs`
included) sit at a large, session-varying, but *roughly constant within a
boot* offset from each other -- session traces disagreeing when
correlated by timestamp is not a validated fact. The **structural**
finding here (multiple stop/start cycles measured entirely within
bpftrace's own single clock, no `dmesg` mixing) is unaffected and is what
`9b6f283` fixes. The specific 117-226ms/5-10x magnitude below should be
read as provisional, superseded by the single-clock value comparison in
"Ground truth via trace_printk".

The stream genuinely does stop delivering completions -- that much is
real. But the real gap lines up almost exactly with `STOP_URBS`, not with
anything mid-stream: what the watchdog is reacting to is the deliberate,
expected silence of `hw_params()`'s own teardown-EP0-restart sequence,
the exact window `urb_stream->stopping` exists to hide from it. And the
watchdog's own *reported* duration is wrong by 5-10x (20-23ms logged
against 117-226ms real) -- `now`/`last`/`age_ns` in `jockey3_watchdog_check()`
are sampled once, outside `urb_stream->lock`, before the `stopping` check;
by the time the tick can act on that sample, `stopping` may already have
gone false again, and the small `age_ns` it computed no longer reflects
how much real silence has actually elapsed. Whether the gap between
sampling and acting is spinlock contention (unlikely -- these critical
sections are brief) or `system_long_wq` scheduling delay behind a rate
change that is itself touching URB-stream state throughout is not pinned
down; it does not change the conclusion, since either way the tick is
acting on data computed before a window it should have been excluded
from.

**Fixed** (`dev/streaming-overhead` `12e5f3c`): `jockey3_watchdog_check()`
now checks `stopping` *before* sampling `last`/`now`/`age_ns`, with all
three taken under the same `urb_stream->lock` critical section, so a
decision is always made from a fresh read taken after `stopping` was
confirmed false, not from a sample taken before an unknown wait. The
existing "read `last` before `now`" underflow-avoidance rule is preserved.

### Ground truth via `trace_printk()`: the reported duration is accurate

The bpftrace-vs-dmesg clock question above made the 117-226ms figures
unreliable, so rather than keep correlating across tools, `50dcb38`
(temporary, `dev/streaming-overhead` only, never to reach `main`) added a
`trace_printk()` inside `jockey3_watchdog_check()`'s locked section,
printing `last_callback_time`, `urbs_started_time`, the `last`/`now` it
actually used, and the computed `age_ms` -- ground truth from inside the
function itself. Comparing this *by value*, not by timestamp, against
what `dev_warn()` printed to `dmesg` sidesteps any clock-alignment
question entirely.

Across the 9 onsets captured this way (1 on `x86_64-prod`, 8 on
`arm64-prod`, both `12e5f3c` manifest-verified builds): the computed
`age_ms` matched the `dmesg`-reported figure **exactly, every time** --
`[22, 20, 21, 22, 20, 21, 20, 23]` computed vs. `[22, 20, 21, 22, 20, 21,
20, 23]` reported on `arm64-prod`, `21` vs. `21` on `x86_64-prod`. Every
one used the `urbs_started_time` fallback (`last_callback_time == 0`) --
genuinely zero completions since the restart, not stale data. **The
watchdog's reported duration is accurate.** `12e5f3c` is doing its job.

### The real question was the threshold, not the measurement

With `dmesg` out of the analysis entirely, the full sequence timing comes
from a single clock (bpftrace's `nsecs` and the driver's own
`ktime_get_mono_fast_ns()` agree to within a few microseconds on the same
tick, confirmed directly). For all 9 onsets, on both platforms:

```
STOP_URBS --~237-240ms--> SET_RATE (112-114ms) --~0.4-0.5ms--> START_URBS --20-23ms--> onset
```

Remarkably consistent across every single traced case. `START_URBS`
follows `SET_RATE` within under a millisecond every time -- resubmission
itself is not the bottleneck. The onset fires **20-23ms after
`START_URBS`, every time**, with zero completions in that window: a real,
highly reproducible cost of bringing the URB ring back up after a rate
change, not an intermittent fault. `JOCKEY3_WATCHDOG_STALL_MS` (20ms) sits
right at the edge of that band, so ordinary rate changes occasionally
cross it and get treated the same as an established stream going silent
-- even though every one of these 9 self-healed on the very next tick
with no restart needed.

**Fixed** (`dev/streaming-overhead` `00d9223`): a separate, longer
threshold, `JOCKEY3_STREAM_STARTUP_GRACE_MS` (200ms), applies whenever
`last_callback_time == 0` -- the existing signal for "no completion since
the last start" -- in both `jockey3_watchdog_check()` and
`jockey3_watchdog_next_delay_ms()`. No new state; ~10x the worst startup
latency observed, so a genuinely wedged device is still caught, just not
mistaken for one on every rate change that happens to land a few ms past
20ms.

**Hardware-validated** (`20260826T205002Z-smoke` x86_64-prod,
`20260826T204838Z-functional` arm64-prod, both 120 changes,
manifest-verified `00d9223`): **zero watchdog "no completion" onsets on
either platform**, down from the ~7-19%/~19-24% baselines. `x86_64-prod`
had no stall-adjacent activity at all. `arm64-prod` had 3 occurrences of a
*different*, pre-existing, unrelated timeout --
`jockey3_pcm_hw_params()`'s own direct post-restart check
(`jockey3_wait_urb_stream_started(..., 50)`, a 50ms budget never touched
by any of the three fixes here) occasionally lands past its own budget on
the same startup-latency tail; each one called
`jockey3_recover_urb_stream(..., "rate change", ...)`, whose pre-existing
early alive-check found the stream already fine and returned with no
restart, no cascade, no reset. Same underlying phenomenon, a different and
already well-behaved consumer of it -- nothing further needed there for
now.

(The ~237-240ms `STOP_URBS`-to-`SET_RATE` gap and the consistent
112-114ms `SET_RATE` duration are both real and worth understanding on
their own terms, but are outside this question's scope.)

### A metric-reading correction: reset counts hide most of the activity

`resets_total_device`/`stalls_per_change_pct` (the latter is scoped to
`capture_stall_hw_params` only) undercount how often the watchdog fires at
all. On the two 300-change validation runs after the fix above:
`x86_64-prod` (`20260826T173215Z-smoke`): 1 reset, but
`watchdog_onset_total=21` (7% of changes), 19 of which self-healed without
even needing a restart. `arm64-prod` (`20260826T173110Z-functional`): 0
resets, but `watchdog_onset_total=57` (19% of changes), 56 of which needed
an actual light restart. Neither run's headline "0/1 resets" figure alone
says anything close to how often this fires -- always read
`watchdog_onset_total`/`watchdog_restarted_total`/`watchdog_recovered_total`
alongside it.

## 2026-08-31: a mid-stream capture stall on `48000->96000`, and a serial-console confound

A different-looking failure surfaced on `x86_64-prod` running JT-RATE-001 at
4 s dwell, on build `55d94ed` (which carries the cold/warm start-grace split
and the `jockey3_stream_streaming_healthy()` evidence gate). Run
`20260831T210749Z-functional` failed: 13 watchdog stalls, **every one on the
`48000->96000` transition**, 2 of them escalating to a full USB reset, which
took `arecord` down with `-EIO` and failed the rate step. The same build had
run clean three hours earlier (`20260831T183059Z`, 0 capture stalls), so it is
intermittent.

### How this differs from the fault documented above

- **Detection point.** The documented stall is caught at `jockey3_pcm_hw_params()`
  -- capture never comes up after the change. These are caught by the URB
  liveness watchdog, *mid-stream, steady-state*, several seconds into the 4 s
  dwell. Cold come-up is fine here: `Capture confirmed alive after 8 ms`.
- **Divider direction.** The documented stall follows the **downward**
  `/256 -> /512` step (toward the lower rate), in both families. This one is on
  the **upward** `/512 -> /256` step, and only in the 48 k family:
  `44100 -> 88200` -- the identical transition on the other oscillator -- was
  25/25 clean in the same run.
- **Direction of every stall confirmed by transition.** Parsing the log, all 13
  `stream stalled (watchdog)` events are preceded by `Rate changed to 96000`
  with `48000` before that. Zero on `96000->44100` (also involves 96 k, also
  cross-divider), zero on `44100->88200`, zero on `88200->48000`. Sweep order
  is `96 -> 44.1 -> 88.2 -> 48 -> loop`, so 96 k is only ever entered from
  48 k -- this run cannot say whether `88200->96000` or `44100->96000` would
  also stall.

The `restart_timing` dataset, split by rate (a dimension added the same day),
makes the shape explicit: cold-start latency is **flat across all four rates**
(p50 8 ms everywhere -- a URB restart is rate-independent), while every warm
restart and every liveness wait in the run is at 96000.

### Ruled out

- **ADC re-lock.** Frank checked the PCM1804 datasheet: the ADC settles within
  31.25-41.67 us of a clock change. Two-plus orders of magnitude below the
  multi-second fragility window. The converters are not the problem.
- **PLL jitter on the 48 k clock.** Both master clocks are crystal oscillators:
  `X2` 22.5792 MHz on the Ploytec schematic (44.1/88.2), `X702` 24.576 MHz on
  the DSP schematic, a 74HCU04 oscillator (48/96). Neither is a PLL output.
- **Distributed-net loading on `X702`.** `X702` feeds the DSP56374 and the
  converters and is distributed across to the Ploytec section, but the copy the
  Ploytec section receives comes through **its own dedicated buffer** -- no
  shared-sink loading, no interference from the DSP or converters on the
  Ploytec-side clock.
- **A general signal-integrity problem on `X702`.** Frank's argument: 48 k and
  96 k run off the *same* buffered `X702` clock; the `/2` that distinguishes
  them happens at the 74HC74 in the Ploytec sample-clock-select circuit. A
  general SI fault on that clock would make 48 k unreliable too. It is not.
  So if the stall is a hardware effect at all, it is **specific to the
  `/256` (96 kHz) branch** of the select circuit -- the tri-state buffer and
  enable that route the un-halved clock, and/or the 74HC74-bypass state.

### Leading hardware hypothesis (if it survives the confound below)

A single corrupted edge -- runt, double, or dropped -- on the Ploytec sample
clock at the instant the tri-state buffers switch **into** the `/256` config on
a `48->96` change. The ISP1583 capture-DMA counter latches one edge too many or
too few; its FIFO write pointer is off by one sample slot; the accumulating
phase error walks the read and write pointers together until they collide and
the capture endpoint stalls, seconds later. This fits every observation:
`48->96` only (the glitch is on the switch *into* `/256`); intermittent (the
glitch only sometimes lands on an edge the ISP1583 is sampling); delayed and
mid-stream (FIFO drift takes seconds to reach collision); ADC settling time
irrelevant (the converters are fine -- it is the *count* that is wrong).
`44100<->88200` clean because the `X2`-side `/256` branch is a separate
instance / has more margin.

The runner-up is the driver/firmware version of the same thing: the capture URB
DMA is (re)started before the switched clock has settled, and 96 k has less
margin than 88.2 k. Same fix direction.

### The confound: `x86_64-prod` has a serial console

`x86_64-prod` (`alsa-test`) routes all kernel messages to a serial console.
With dynamic debug on -- which it has been for the last several runs, to feed
`restart_timing` -- every `dev_dbg` the driver emits is shifted out the UART
**synchronously** before the calling context continues. At 115200 baud an
80-120 character line is roughly 8 ms; the rate-change and recovery paths emit
15-20 lines each, so **100+ ms of non-deterministic latency lands right where
the driver is programming a rate and (re)starting streaming**, on x86_64 only.

This is not a side issue. It plausibly explains the whole thing:

- **The recovery poll loop instruments itself into escalation.**
  `jockey3_wait_urb_stream_started()` polls `jockey3_stream_streaming_healthy()`
  every ~1 ms, and both that helper's `dev_dbg_ratelimited` lines and the
  loop's own `dev_dbg` go out the serial port *between* checks. A stream that
  is genuinely recovering can have its `warm_start_grace_ms` (150 ms) window
  expire while the CPU is busy writing "cadence plausible but last completion
  is stale" to the UART -- and then it escalates to a USB reset. The "x9"
  repetition of that line in the failing run is the loop doing serial I/O, not
  the capture endpoint being nine-times-dead.
- **It inverts the platform reference.** `x86_64-prod` was historically the
  rock-solid reference and `arm64-prod` the one that needed occasional
  restarts. That flipped the moment dyn_dbg plus the serial console came on:
  the arm64 host has no serial console, so its debug output only reaches the
  ring buffer.
- **It contaminates `restart_timing`.** The measurement mechanism is
  `dev_dbg` lines; on this host each one is ~8 ms instead of ~us. Any x86_64
  number taken with `console_loglevel >= 7` and dyn_dbg on is suspect. Treat a
  lowered console loglevel as a precondition for the dataset, not just
  "dyndbg on".

### Angles to test, in order

1. **Re-run the 96 kHz sweep on x86_64 with the console quiet.** Set
   `console_loglevel` below 7 first (`tests/hw/priv/jockey3-testctl
   printk-console 4`). Debug still fills the ring buffer and the `kmsg.log`
   whole-run capture (`dmesg --follow`), it just stops being shifted out the
   UART in the driver's critical path. If the stalls vanish, this was a
   measurement artifact and the driver behaviour is fine. If they persist, the
   fault is real and the serial console merely amplified it into visibility.
2. **If real: scope the Ploytec sample clock across a `48->96` change.** Probe
   the 74HC74 output / the tri-state select outputs, with a `bpftrace` or
   `/dev/kmsg` marker for when the driver issues `SET_RATE` and when it
   restarts the URBs. Look for a glitch on the switch edge and whether it falls
   inside the URB-restart window. That single trace separates hardware from
   driver and sizes any extra settle delay that would cover it.
3. **Re-run `sweep_order=as-given rates=[96000,48000,96000,44100]` at >=4 s
   dwell, console-quiet, on the current build.** Apples-to-apples against the
   2026-08-16 table -- which was short-dwell ("smoke", ~1 s) and only ever
   measured the *come-up* stall, never a sustained 96 kHz dwell, and predates
   the watchdog acting on mid-stream stalls. This says whether `48->96` is
   genuinely a new failure mode or one that was always there and unwatched.
4. **OpenVizsla: compare the `48->96` timing, vendor vs our driver.** The macOS
   corpus already holds ~9 `48000->96000` transitions (see the 08-17 section).
   Pull the `SET_RATE` burst timing and the post-change quiet window from those
   and from a fresh Linux trace through `re/usb/extract_events.py` ->
   `rate_change_stream_timing.py` / `rate_burst_profile.py`, and see whether
   the vendor leaves a longer or differently-placed window on the upward
   in-family step specifically. Caveat: the driver's sequence has moved since
   the 08-17 comparison, so a fresh Linux capture is needed, not the old one.
5. **Try divergence 1 (send `SET_STATUS` / `0x49` on a rate change) and
   re-test at 4 s dwell, console-quiet.** Re-arming the ISP1583 capture DMA
   against a settled clock is exactly the remedy an off-by-one count would
   need; it is already the most-actionable item from the vendor comparison.
6. **Driver settle delay.** `ploytec_set_rate()`'s delays are fixed and
   rate-independent (50 ms pre-write, 10.5 ms, 50 ms post-verify), inferred
   from vendor traces that may not have covered this transition under sustained
   load. A longer settle before `jockey3_start_urbs()`, conditional on the
   `X702` family and the upward step, is a cheap thing to try if 5 does not
   land it.

### Interaction with the evidence gate

Whatever the root cause, the *response* changed with `b20b77b`. The old
`jockey3_check_urb_stream_alive()` accepted the first trickle completion after
a light restart as "recovered" and never escalated; the evidence gate correctly
refuses a trickle-then-silence and escalates to a USB reset. So a marginal
96 kHz capture recovery that `0a4822c` would have limped through now becomes a
reset -- and, in JT-RATE-001, a failed rate step. That is the gate working as
designed; it also means "was `48->96` clean before?" cannot be answered by
comparing pass/fail across builds, only by comparing the underlying
`watchdog_onset` / stall counts.

## Open questions, in the order worth attacking

1. ~~Measure per-change incidence on each branch, one variable at a time.~~
   Done 2026-08-15/16.
2. ~~Direction or clock family?~~ **Resolved 2026-08-16: direction, specifically
   the flip-flop's divide-ratio transition from /256 to /512. Not the NAND
   oscillator mux** -- see above. Power-of-two ratio remains entangled with
   divider-ratio change and cannot be separated on this hardware.
3. **Does the device wedge under sustained resets, and does spacing changes
   out prevent it?** New 2026-08-16, see above. `gap_seconds=3` is the
   instrument already identified in the 08-15 section for a related question
   (whether an unwaited reset overlapping the next change matters) and is the
   most direct test: if the wedge does not recur with a gap, back-to-back
   resets are implicated; if it does, the cause is elsewhere (cumulative
   device-side state, e.g. a counter or buffer that does not reset per-cycle).
4. ~~**What does the wire show during a failing rate change?**~~ **Answered
   2026-08-17** -- capture IN never produces a single packet, while playback
   OUT resumes normally and EP0 reports no fault. See the 08-17 section. The
   dedicated capture this question reserved was not needed; the episodes were
   already in `capture_2026-08-17_linux_ratechange`. One residual: the parser
   discards NAKs, so silent-versus-NAKing is still undetermined.
5. ~~**Why does our sequence take 166-211 ms against macOS's 103-142 ms?**~~
   **Withdrawn 2026-08-17 -- the premise was a measurement artifact.** See
   the 08-17 section: our sequence is 121.6-123.6 ms, marginally faster than
   macOS. There was never a gap to explain.
6. **Does sending `SET_STATUS` unconditionally fix the capture stall?** New
   2026-08-17. The single most actionable item to come out of the vendor
   comparison -- see divergence 1 in that section.
7. **Is the `48000->96000` mid-stream stall real, or a serial-console
   artifact?** New 2026-08-31. Re-run console-quiet first (angle 1 in that
   section); everything else is downstream of that answer.

## Next steps: characterize vendor up/down behavior before touching the driver

The temptation after "it's the divide-ratio direction" is to start changing
`ploytec_set_rate()` -- longer waits before a downward change, an extra poll,
a different burst shape going down versus up. Resist that until there is a
reference to change *toward*. `ploytec_set_rate()` already replicates the
macOS/Windows burst shape, endpoint order and quiet-window timings faithfully
(see its kernel-doc and `init_timing_comparison.md`), and neither vendor
driver stalls, ever, in the existing corpus -- including on downward changes.
So either the vendor sequence contains something direction-dependent that
was missed the first time through (`init_timing_comparison.md` was written
for the cold-boot fault and never specifically asked "does the vendor treat
downward and upward rate changes differently"), or it doesn't, and the
answer lies somewhere this document hasn't looked yet (host-side URB
resubmission timing after `SET_STATUS`, for instance, rather than the EP0
sequence itself).

**The plan, in order:**

1. **Re-run the existing macOS/Windows corpus through a direction lens.**
   Done -- `usb/classify_rate_transitions.py` (new, 2026-08-16) parses every
   `GET_RATE(ep=none)`/`SET_RATE` pair in `usb/openvizsla/*_events.txt` and
   classifies it by direction and by divide-ratio class (`within` /
   `cross` / `lateral`), reading the *old* rate from the query the vendor
   always issues rather than assuming it from context -- which matters,
   since `capture_macos_rate_change.txt` starts mid-session with no
   enumeration in view. Run it with `--summary` for counts, no arguments
   for the full per-event listing, `--csv` to export.

   The corpus turned out thinner than the previous pass through it assumed.
   `capture_macos_rate_change` has **7** transitions, not the 5 the pasted
   LLM analysis in `capture_macos_rate_change_analysis.md` describes -- it
   silently skips two events and mis-transcribes a third (claims event 7 is
   `96000->48000`; the trace's own `GET_RATE` says its old rate was 44100).
   That is the concrete instance of the caution already on record about that
   file: it is a lead, not a source. Re-deriving from the parsed events
   instead of the prose changed the answer, immediately, on the first check.

   What actually exists, corpus-wide (macOS + Windows, `--summary` output):

   | direction | divider_class | n | pairs |
   |---|---|---|---|
   | up | lateral | 7 | `44100->48000`, `88200->96000` |
   | up | within | **1** | `48000->96000` (Windows only) |
   | up | cross | 18 | `44100->96000` (x16), `48000->88200` (x2) |
   | down | lateral | 1 | `48000->44100` (macOS only) |
   | down | within | **0** | -- |
   | down | cross | 3 | `96000->44100` (1 macOS + 2 Windows) |

   **`down`/`within` -- the exact class that stalls 55-60% of the time on
   this driver (`96000->48000` and its mirror `88200->44100`) -- has zero
   examples on either platform in the entire corpus.** Every claim this
   document could make about "what the vendor does differently going down"
   currently rests on `down`/`cross`, and on one specific pair
   (`96000->44100`) at that; `88200->48000`, the other `down`/`cross` pair,
   also has zero examples. This is a real gap, not a case of the existing
   traces being under-analyzed, and it means the corpus cannot currently
   answer the direction question at all for the stall's own strongest
   cell -- see "Captures needed" below before drawing a conclusion from
   what exists today.
2. **Compare, per direction, everything the previous macOS-only comparison in
   `capture_macos_rate_change_analysis.md` asserted but never checked against
   Windows or against direction:** burst length (5-7 writes, stated as varying
   "within a single platform" -- does it vary *with direction*?), the
   position and width of the ~10 ms and ~50 ms quiet windows, and the
   `resume_out_ms` / `resume_in_ms` figures from
   `rate_change_stream_timing.py`. That file is worth naming directly: it
   reads as an LLM chat transcript pasted in wholesale, mixes real trace
   excerpts with unverified speculation ("almost feels like a hack to cope
   with obscure firmware flaws"), and its pseudo-code does not match the
   actual event sequence in its own trace closely enough to trust without
   re-deriving. Treat its data as a lead, not a source -- redo the
   classification from the parsed events, the same standard the rest of this
   document holds itself to.
3. ~~**Only if a direction-dependent asymmetry turns up in the vendor sequence
   itself**~~ **Resolved 2026-08-17: no asymmetry turned up.** The second
   branch of this step is the one that applies -- see the 08-17 section. The
   investigation moves off `ploytec_proto.c`'s EP0 sequence, with the one
   exception of the unconditional `SET_STATUS` in `ploytec_start_streaming()`,
   which is a divergence from the vendors in *both* directions rather than a
   direction-dependent one. Original wording kept below for the record:

   **Only if a direction-dependent asymmetry turns up in the vendor sequence
   itself** does this motivate a specific, targeted change to
   `ploytec_set_rate()` -- e.g. a different wait or an extra status poll on
   the /256->/512 transition specifically. If no asymmetry turns up, that is
   also a result: it would mean the vendor drivers get away with an
   identical EP0 sequence in both directions and something else (host URB
   timing, buffer state left over from the previous rate, or a firmware race
   that is simply less likely to lose when driven from a different USB host
   controller) accounts for the difference, which points the investigation
   at `jockey3.c`'s URB resubmission path after a rate change rather than at
   `ploytec_proto.c`'s EP0 sequence.
4. **Any change that does get made is validated the same way this session's
   findings were: `JT-RATE-001` with `sweep_order=as-given`, both cells of a
   direction, before/after, at n>=60 per cell** -- n=25 was not enough to
   trust the 60/40 split this session, and a real fix has to clear the same
   bar the noise did.

The wedge investigation (`gap_seconds=3`) and the vendor comparison are
independent and can proceed in either order; the vendor comparison is pure
analysis of already-captured traces and needs no hardware time, so it is the
better use of time between hardware sessions.

## Captures needed

OpenVizsla capture is a manual, one-trace-at-a-time process and the raw
files are large, so this is deliberately a short list, not "capture
everything and see." Each entry exists because `classify_rate_transitions.py`
found the corpus has zero examples of it, not as a hedge.

**Priority 1 -- ~~blocking, currently zero examples anywhere~~ SATISFIED
2026-08-17** by `capture_2026-08-17_macos_ratechange` (11 `down`/`within`, 9
`up`/`within`) and `capture_2026-08-17_win_ratechange` (11 and 11). Both
requests below are closed; the analysis is in the 08-17 section above, and the
answer to the question they were taken to settle is that the vendor sequence
does not depend on direction at all.

- ~~**macOS: `96000<->48000`, cycled back and forth 4-6 times in one capture.**~~
- ~~**Windows: the same pair, the same way.**~~

**Priority 1b -- new 2026-08-17, the remaining gap is on our own side:**

- ~~**Linux: `96000<->48000` cycled until it stalls,** on the current driver.~~
  **Overtaken by the fix** -- it no longer stalls, so there is no failing
  change left to capture. What would still be worth one trace is the
  *working* sequence on the current driver, to confirm on the wire that our
  terminator is now byte-identical to the vendors' and that capture resumes
  in the vendor's 1-21 ms window rather than our old 52-71 ms. Lower priority
  than it was, since the metric already answered the question that mattered.
  Original request, for the record:
  The wire evidence for a failing change (08-17 section) comes from
  `down`/`cross` episodes, because the sweep in
  `capture_2026-08-17_linux_ratechange` contains no `down`/`within` at all.
  Both classes cross the /256->/512 divider transition and are very likely the
  same fault, but that is currently an assumption. A capture with
  `JT-RATE-001` restricted to `96000<->48000` would confirm it directly.
  Worth taking together with a re-parse that **retains NAKs**, which is the
  only way to tell a silent capture endpoint from one NAKing every poll.

**Priority 2 -- thin, one pair covers the whole `down`/`cross` class today:**

- **One platform, `88200<->96000<->44100<->48000` cycled a few times**, or
  more simply `88200<->48000` back and forth 4-6 times if the capture can be
  targeted that precisely. This is the *other* `down`/`cross` pair --
  `96000->44100` has 3 samples, `88200->48000` has none -- and checks whether
  the vendor sequence (if it turns out to differ from the up-going one at
  all) does so consistently across both `down`/`cross` pairs or is specific
  to one. Do this only after priority 1 is in and has been looked at; if
  priority 1 already shows no direction asymmetry, this pair is unlikely to
  either, and is not worth the trace size on its own.

**Not requested -- existing coverage is enough:** `lateral` (7 up, 1 down)
and the well-covered `up`/`cross` pair (`44100->96000`, n=16) do not need
more. `48000<->44100` and `88200<->96000` are the two `lateral` pairs and
the divide-ratio hypothesis predicts they never stall regardless of
direction -- worth a hardware confirmation via `JT-RATE-001` eventually
(untested combination: neither the interleaved nor the as-given sweep so far
has isolated a lateral-only transition), but that is a hardware question,
not a reason to spend more OpenVizsla time on the vendor side.

**Sizing rationale:** JT-RATE-001 needs n>=60 per cell because the fault it
measures is probabilistic. A vendor EP0 sequence is not -- the existing
corpus already shows burst count and quiet-window width stable to under 4%
across 58 sequences (`init_timing_comparison.md`). 4-6 repeats per pair is
enough to see whether the sequence structure depends on direction at all; if
it does, that structure will show up on the first or second repeat, not the
fortieth.

**Keeping the capture small:** dwell only as long as it takes to see the
handshake complete and confirm streaming resumed -- a few seconds per rate,
not sustained playback. `capture_macos_rate_change.txt`'s own origin note
describes stripping "the many repetitive 512 byte audio in/out packets"
after the fact; avoiding recording minutes of steady-state audio in the
first place is cheaper than stripping it afterward. Run every new capture
through the existing pipeline (`parse_openvizsla.py` -> `extract_events.py`)
to produce the `*_events.txt` `classify_rate_transitions.py` reads, and give
it a metadata sidecar via `make_trace_sidecar.py` -- host, OS and driver
version, and the objective (which pair/direction this capture exists to
fill), same as every other trace in the corpus.

## Reproducing

```sh
cd tests/hw && ./runner.py --case JT-RATE-001 --unattended     # ~3.5 min
python3 re/usb/rate_change_stream_timing.py                    # vendor timings
python3 re/usb/classify_rate_transitions.py --summary          # vendor direction/divider coverage
python3 re/usb/rate_burst_profile.py --summary                 # per-burst sequence shape
```

`rate_burst_profile.py` is the right tool for any question about sequence
*shape*, and the only correct one for driver traces, whose events can hold
more than one rate change; `classify_rate_transitions.py` remains fine for
coverage counts over the vendor traces.

The defaults are the reset-branch arm: capture open, 4 s per rate, no gap. Use
`--param` for the others (see above). `./selftest.py` covers the stall
attribution and the parameter parsing without hardware.

Both vendor-side scripts read the checked-in `openvizsla/*_events.txt` and
`*_parsed.txt`, so neither needs hardware or a fresh capture to run.
