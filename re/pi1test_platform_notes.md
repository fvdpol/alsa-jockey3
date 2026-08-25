# pi1test (Raspberry Pi 1B, armhf) platform notes

Running knowledge base for what works and what doesn't on `pi1test`
specifically, and why -- kept separate from the driver-generic docs because
several of these are properties of this one board (single-core ARMv6,
dwc2 sharing a hub with onboard Ethernet), not of the driver or of `armhf`
in general. Per the platform tier policy (`docs/test_strategy.md`, target
table), `armhf-prod` is best-effort: the bar is "does not crash, hang, or
oops"; degraded throughput and occasional xruns are accepted, and the
driver's URB/interrupt architecture is deliberately not being changed to
chase better numbers here.

## Board-level cause already on record: the dwc2 SOF-interrupt latch

Investigated when `pi1test` first showed high interrupt rates and elevated
system time with the Jockey 3 attached: the BCM2835's dwc2 USB controller
permanently latches its SOF (start-of-frame) interrupt on at 8000/s, because
the Pi 1B routes its onboard Ethernet through the same onboard USB hub,
which keeps a periodic status-change endpoint always active. This steals CPU
time from URB completion handling on a CPU that only has one core to give,
independent of anything the driver does. This is the standing root-cause
explanation for playback URB stalls observed on this board -- see below.

## 2026-08-24/25 smoke run (`armhf-prod`, run `20260825T002415Z`)

Full run: 8 pass / 4 fail / 1 blocked. Broken down by what each failure
actually indicates:

### Capture-arm rate changes: clean

`JT-RATE-001`'s capture side measured rate ratio 0.955-1.000 across all 6
rate changes, zero stalls, zero resets. The capture-arm `hw_params()`
rate-change path is working correctly on this hardware. This is the
baseline that makes the playback-side finding below a real asymmetry rather
than "everything is just slow here."

### Playback: repeated watchdog stalls, matching the known SOF-latch cause

Same run, playback side: rate ratio down to 0.664 (33.6% clock error at
worst), 4 watchdog-triggered stalls (`Playback URB stream stalled: no
completion for NN ms`). Consistent with the SOF-interrupt-latch finding
above -- the CPU is not getting scheduled onto URB completion work quickly
enough. Not a driver defect; not actionable per the platform tier policy.

### One reset that timed out and dropped the device off the bus, then self-recovered

At the 44100 Hz change (`JT-RATE-001#change2-44100`), three watchdog stalls
escalated to `queuing full USB reset (watchdog)` within about two seconds.
The *third* reset itself timed out (`Timeout waiting for reset completion`)
and the device dropped off the bus briefly -- two `usb 1-1.5.1: device
descriptor read/64, error -71` (EPROTO) lines -- before a further reset
completed and the device came back. No power cycle was needed, and the
remaining 4 rate changes in the same run completed cleanly with no further
resets. This is a step beyond the routine "stall then light retry"
pattern -- a reset that itself doesn't complete, however briefly, is more
than ordinary slowness -- but it self-healed and stayed inside the
crash/hang/oops bar. **Status: noted, not chased.** Worth checking whether
it recurs in future runs; a single occurrence isn't enough to act on.

(The `resets_total_device` metric read 0 for this run despite the three
`reset high-speed USB device` lines in dmesg -- not a miscount. That metric
is deliberately scoped to resets escalated from the rate-change test's own
`hw_params()`/`prepare()` light-retry paths, not the independent watchdog's
resets; see the `CONTEXT` table in `tests/hw/cases/rate_change.py`.)

### `JT-PCM-002` / `JT-PCM-004`: 1-2 xruns on a plain 44.1 kHz check

Trivial by count and squarely inside what the platform tier policy already
accepts for this board.

### `JT-PROBE-001`: one `rmmod: Module ... is in use` out of 9 unload attempts

`probe_cycle.py` (the case behind `JT-PROBE-001`) never opens a `/dev/snd/*`
node itself -- it reads only `/proc/asound` between load and unload -- so
nothing in the test's own code held the extra module reference that made
`rmmod` fail. Frank's read, and the most likely explanation: no sound
server (no PulseAudio/PipeWire/JACK on this machine -- confirmed via
`env.py`'s capability probe, all three read `false`/absent), so the
remaining suspect is **udev** -- specifically its own brief open of the
newly-appeared card's control node while handling the hotplug ADD event
(e.g. an `alsactl`/udev-rule-triggered restore), which on a single-core
700 MHz board may not have finished and closed its file descriptor by the
time the case calls `rmmod` immediately after checking substreams. On the
faster targets, that udev-triggered helper finishes long before the test
harness gets there; on `pi1test` it can still be running.

**Mitigation implemented, 2026-08-25**: `probe_cycle.py` gained a
`settle_delay_s` parameter (default `0`, `tests/hw/catalog.yaml`) -- a fixed
sleep inserted after a load's substream check and before the unload attempt,
excluded from the `unload_ms` timing. `tests/hw/profiles.yaml` overrides it
to `3` seconds for `armhf-prod` in every profile that runs `JT-PROBE-001`
(`smoke`, `functional`, `regression`, `soak`); every other target keeps the
default of `0`, so this changes nothing anywhere else. Frank's call: a fixed
sleep rather than polling `lsmod`'s use-count column, configurable per case
rather than hardcoded to the target. **Hardware-confirmed, 2026-08-25**: the
next smoke run on `pi1test` (`settle_delay_s=3` applied) passed `JT-PROBE-001`
clean on all 3 iterations -- no further `Module ... is in use`.

## Known test-harness cost specific to this board

Already characterized in an earlier session (see `.claude/session-state.md`
history / `tests/hw/lib/priv.py`, `tests/hw/lib/yamlio.py`): `sudo`/PAM
round-trips on `pi1test` cost seconds per call (measured up to ~10.8s for a
single `sudo -n jockey3-testctl` invocation), and YAML parsing without the
`libyaml` C extension cost tens of seconds more before it was fixed. Both
are userspace/tooling costs on this specific board, not driver behavior --
already fixed where they were fixable (`yamlio.py`), documented where they
aren't (`sudo`/PAM overhead, not yet root-caused further, parked by Frank's
own call as not worth the complexity for one weak target).

## 2026-08-25: 88200 Hz capture wedges `arecord` for ~46 minutes, board briefly unreachable over ssh

During the same smoke run that validated the `JT-PROBE-001` fix above,
`JT-AUDIO-002` (output/loopback check, running for the first time on this
board because a loopback cable was newly connected) got through 44100 Hz and
48000 Hz cleanly enough (with the usual handful of xruns) and then hung at
88200 Hz. The case's own `record_floor()` calls `arecord` under
`subprocess.run(..., timeout=duration+30)` -- **that timeout did fire**
(`TimeoutExpired ... timed out after 31.5 seconds`), but the case's total
reported duration was **2769.8s (~46 minutes)**, not ~32s. That gap means
`subprocess.run()`'s internal `kill()` did not make the `arecord` process
exit -- `Popen.wait()` blocked for the rest of that time, which only happens
when the target process is stuck in an uninterruptible kernel wait (D state)
that even `SIGKILL` cannot interrupt until the kernel side unblocks it. So
the real event is not "the test was slow at 88200 Hz" -- it is **an
`arecord` process wedged inside the kernel for the better part of an hour**,
almost certainly blocked somewhere in the capture URB/ALSA path at that
rate. It did eventually clear on its own, without a power cycle.

Immediately after, `JT-AUDIO-005` found `jockey3-testctl status` itself
timing out (20s), and shortly after that **ssh to `pi1test` stopped working
entirely** -- `ping` succeeded (2ms RTT, kernel/network alive) but new ssh
connections timed out during the banner exchange, even with a 60s connect
timeout. Frank had a local console open throughout and confirmed via
`vmstat 1` the board was not deadlocked: `id` held at 30-45%, `b` (blocked)
stayed at 0, but `in` (interrupts) was running **~30,000-33,000/s
sustained**, with `sy` (system time) at 50-60%. That is consistent with
packet-interval math in `jockey3.c`'s watchdog comment (226.8 us/packet at
44100 Hz vs. 83.3 us at 96000 Hz -- 88200 Hz sits near that fast end, so
roughly double the completion-interrupt rate of 44100 Hz), on top of the
board's known SOF-interrupt-latch baseline (see above) -- together enough
sustained interrupt load that a brand-new sshd connection kept losing the
scheduling race to complete its banner exchange, even though nothing was
truly hung.

**Resolution, confirmed live**: Frank ran a plain `aplay -D hw:RJ3 -r 44100
-c 4 -f S24_3LE -d 1 /dev/zero` from the console to force
`jockey3_set_rate(44100)` (URBs run free for the device's lifetime, so any
open at a new rate reprograms and restarts them -- see `jockey3.c`'s `DOC:`
blocks). `vmstat` immediately after: interrupts dropped from ~30,000/s to
~16,700-16,900/s, `sy` fell to 10-20%, `id` rose to 84-91%. The board became
reachable over ssh again within moments. **No power cycle was needed.**

### What the captured dmesg actually shows

Frank grabbed `dmesg | tail -80` and `/proc/interrupts` while the board was
still in the high-interrupt state, before the rate-change fix -- saved here
as [`pi1test_2026-08-25_88200hz_dmesg.txt`](pi1test_2026-08-25_88200hz_dmesg.txt)
and [`pi1test_2026-08-25_88200hz_interrupts.txt`](pi1test_2026-08-25_88200hz_interrupts.txt).
Two things in it sharpen the finding well past "the board got slow":

1. **A ~17.2-minute total silence in the kernel log**, immediately after the
   `JT-AUDIO-002#1` marker (`165768.757685`) and before the next line at all
   (`166803.210316`, a `Playback URB stream stalled ... restarting URBs to
   recover`). The URB watchdog polls at `JOCKEY3_WATCHDOG_POLL_MS` (1000 ms)
   and would log the instant it saw a real stall on an active stream, so 17
   minutes of nothing from it points at capture never getting far enough
   into the URB-streaming state the watchdog even watches -- i.e. stuck
   somewhere in the open()/`hw_params()`/`prepare()` setup path, not in
   steady-state streaming.

   Checked against the actual code rather than left as a guess: it is
   *not* a hung EP0 control transfer specifically. `ploytec_set_rate()`'s
   `usb_control_msg_send()` calls all use the bounded `PLOYTEC_CTRL_TIMEOUT_MS`
   and call `dev_err()` on failure (`ploytec_proto.c` "Failed to set rate on
   EP ..."); no such line appears anywhere in the captured log, so whatever
   rate-set traffic happened either succeeded quickly or was never reached.
   The stuck point is therefore somewhere else in the setup path -- a
   `rate_mutex` wait, a `usb_submit_urb()` call blocking at the
   host-controller level, or something further down in alsa-lib/`arecord`
   itself before it gets far enough to open the device -- not narrowed down
   further than "before the URB watchdog's territory, and not a logged EP0
   failure."
2. **Two full USB disconnect/re-enumerate cycles happened right before the
   run's own cases even started** (`165292`-`165314`, device numbers
   20->21->22), each preceded by a burst of `Failed to resubmit
   playback/capture URB: -19` (ENODEV -- resubmission racing a
   disconnect-in-progress, not a fault of its own). This is 17 seconds of
   genuine bus instability that predates `JT-AUDIO-002` and was not
   mentioned in the run's own classified output; whether it is related to
   the later 88200 Hz wedge or an unrelated, already-recovered-from blip is
   not established -- noted here so it is not lost.
3. **After whatever was stuck let go**, the log shows five more playback
   watchdog stalls over about 14 seconds (`168285`-`168299`), with recovery
   times noticeably longer than the single isolated stall seen earlier in
   the same run during `JT-RATE-001` (up to 524 ms here vs. 23 ms there) --
   consistent with playback URB submission having been starved and needing
   several cycles to catch back up once whatever was blocking things
   cleared, rather than a single clean recovery.

None of this is proof by itself -- it is circumstantial, from one occurrence
-- but it does narrow the search: **whatever got stuck was somewhere before
the URB watchdog's territory (open/hw_params/prepare, or lower), and it was
not a logged EP0 control-transfer failure**, rather than "the driver's URB
loop falls behind at high rates." If this recurs, the highest-value capture
is a stack dump of the stuck process (`cat /proc/<pid>/stack`) and `dmesg`
taken *while it is still stuck*, before anything (including a rate change)
clears the state -- this occurrence only has state from before the fix, not
during the actual wedge, so which specific wait it was stuck in is still
unknown.

### Follow-on: the load itself became its own investigation

The interrupt/system-time numbers in this section are the motivating
observation behind `re/streaming_overhead.md` (the study) and
`re/streaming_overhead_experiments.md` (the E1-E4 plan), which look at
reducing the host cost of the driver's continuous URB streaming in general --
transfer coalescing, idle rate downshift, on-demand streaming. Two cautions
for anyone quoting numbers from here:

- **The 88.2 kHz `vmstat` reading above was taken during the wedge**, with
  `arecord` in D state and recovery cycles running. It measures a fault, not a
  streaming baseline, and no interrupt-rate model should be fitted to it. The
  44.1 kHz reading taken after the recovery is the clean one.
- Establishing proper baselines is exactly what experiment E1 exists for.

### What this settles, and what it doesn't

- This is now real, first-hand evidence for the `armhf-prod` "high sample
  rates are unreliable" question `tests/hw/targets.yaml` had marked
  UNVERIFIED -- but it settles only that **88200 Hz capture can wedge on
  this board**, not the broader question of whether sustained high-rate
  streaming is viable here once already running, and not which specific
  wait it got stuck in. Whether this reproduces without the loopback case's
  specific sequence (floor measurement immediately following two prior
  rate/xrun-heavy passes) is untested.
- **Mitigated in the test suite, 2026-08-25**: `tests/hw/profiles.yaml`'s
  `smoke` and `regression` profiles now restrict `JT-AUDIO-002` to `[44100,
  48000]` on `armhf-prod`, matching the restriction `functional` already
  had (added at some earlier point for the same underlying reason, but
  never carried over to `smoke`/`regression` -- that gap is exactly what let
  this run hit 88200 Hz at all).
- **Not yet root-caused further.** No hypothesis here has been confirmed
  against the actual code path with a live capture during the stuck state --
  everything above is read off the shape of a post-hoc log, and the specific
  wait involved remains unknown.

## Log capacity gap

This run's own preflight check flagged `pi1test`'s `log_buf_len` as 128K,
below the 4M other targets carry. Some markers or diagnostics may have been
silently dropped mid-run as a result. Worth setting `log_buf_len=4M` on this
board's kernel command line too, to keep future deep-dives here as
trustworthy as on the other targets.
