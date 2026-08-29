# On-demand PCM streaming: starting the URBs only when something is using them

> **CLOSED 2026-08-29.** The gate failed on real hardware: MIDI IN does not
> survive the PCM URB rings being stopped. On a `Jockey 3 Remix` on
> `x86_64-prod`, with the driver's own dev_dbg confirming the rings were
> stopped and no MIDI IN URB error of any kind in the log, thirty seconds of
> jog wheel, fader and button input produced zero bytes on the rawmidi input.
> An OpenVizsla trace of a repeat run confirms the mechanism precisely: the
> MIDI IN URB stays submitted and the host polls it continuously the whole
> time (~46,000 NAK'd attempts/s, no gap) -- the pipe stays open, the
> firmware simply has nothing to send while its audio engine is idle. As
> specified from the start, that closes the feature -- see "2026-08-29: the
> gate fails" and the OpenVizsla section below. Phase 1's code stays on
> `dev/on-demand-streaming`, unmerged; nothing further is planned against it
> pending Frank's decision on what becomes of the branch.

This document is the working document for the feature. It records what is being
built and why, what the change touches, and -- as evidence arrives -- what the
hardware says. It supersedes experiment E3 in
`re/streaming_overhead_experiments.md`; see "What happened to E3" below.

## The feature, in one paragraph

Today the playback and capture URB rings run continuously from `probe()` to
`disconnect()`, whether or not anything is using the device. On-demand streaming
keeps them stopped while the device is idle. They start again on a PCM open or
on the first outgoing MIDI byte, and stop after a configurable idle period
(`idle_timeout`, default 600 seconds) during which no PCM substream was open and
no MIDI OUT traffic was seen. MIDI IN keeps running the whole time.

## Two decisions that shape the whole design

**A restart is treated as a cold start, not as a cheap resubmit.** The study's
E3 gate asked whether the device could resume bulk OUT and IN after a
multi-second gap with *no* EP0 traffic, inside a 20 ms budget, on the theory
that on-demand is only viable if restarting is nearly free. That framing is
abandoned. Restarting goes through `jockey3_set_rate(cold_init = true)` followed
by a URB start -- the same sequence `jockey3_restore_device()` uses, which is the
only restart path this driver has ever proven on hardware. It costs hundreds of
milliseconds, and that is accepted: a delay on the first open, or on the first
MIDI byte after ten idle minutes, is not a user-visible problem.

**Because starting is expensive, stopping is deliberately conservative.** The
600 second default follows directly from the paragraph above. This is not a
power-management feature that tries to idle between tracks; it is a feature that
notices the device has been forgotten about. A short timeout would trade a
constant, cheap cost for an occasional, expensive, and -- on this firmware --
not entirely reliable one.

## What the payoff actually is, and what it is not

`re/streaming_overhead.md` Part 5, point 5 makes the strongest argument for this
lever: coalescing does not reduce bus activity, because the host controller keeps
PING/NAK-ing the endpoint either way, so the USB link never leaves L0. Only
lever 3 stops that, and the study calls it lever 3's one genuinely unique
benefit.

**This design does not deliver that benefit.** The MIDI IN URB stays submitted
across the idle period by design -- that is the whole point of keeping the
control surface alive -- so the host controller keeps polling EP 0x83 and the
link does not leave L0. Anyone reading the study and expecting a power win from
this work will not get one.

What survives is the CPU and interrupt saving, and it is real. An idle bulk IN
URB with no data to deliver sits NAKing and produces almost no completions, so
the idle completion rate drops from lever 4's 2,400/s at 96 kHz to approximately
zero. That is the number that matters on `pi1test`, where the motivating
observation was 30,000-33,000 interrupts/s and an ssh banner exchange that could
not complete (`re/pi1test_platform_notes.md`).

Quantifying that saving is Phase 3 work; nothing here is measured yet.

## The gate: does MIDI IN survive?

**Does the device keep delivering MIDI IN on EP 0x83 while the PCM URBs are
stopped?**

Nothing in the OpenVizsla corpus can answer this. Neither vendor driver ever
stops streaming; `capture_2026-08-13_macos_poweron_noapp` shows macOS streaming
141,524 packets on EP 0x05 with no application open at all
(`re/usb/init_timing_comparison.md`, Finding 4). The reference implementation's
answer to "what do you do when nothing is using the device" is "keep streaming",
so there is no trace of a device with a stopped audio engine and a live MIDI
endpoint.

It is answered by experiment, not inference, and it is a kick-out criterion. The
Jockey 3 is a DJ controller whose primary function is MIDI. A controller whose
jog wheels, faders and buttons go dead whenever audio is closed is a functional
regression, not a tradeoff. If the firmware only pumps EP 0x83 while its audio
engine is running, this feature is closed and the driver keeps streaming
continuously.

Note the asymmetry: the *other* direction needs no gate. Host-to-device MIDI
rides byte 480 of the playback packet, so with the playback URBs stopped that
path does not physically exist -- and it does not need to, because an outgoing
MIDI byte is one of the two start triggers.

### 2026-08-29: the gate fails

`JT-MIDI-008`, run `x86_64-prod/20260829T171709Z-functional`, device `Jockey 3
Remix` (`200c:1037`, firmware `1.0.6`), operator Frank.

The case forced a fresh probe (unbind/bind), set `idle_timeout` to 5 s, wrote a
marker, and waited. The kernel log:

```
[341430.479022] JT-MARK JT-MIDI-008-wait e9521b733e52
[341435.474661] snd-reloop-jockey3 1-13.1:1.0: On-demand: stopping PCM streaming, idle for 5035 ms (idle_timeout=5 s)
```

4995.6 ms after the marker -- within a rounding error of `idle_timeout`, which
is what confirms `jockey3_idle_work()`'s remaining-time rearm is computing
correctly rather than drifting on a flat poll. The operator then moved a jog
wheel, a fader and pressed several buttons for the full 30 s watch window.

**Result: `bytes_received_while_stopped: 0`.** Zero bytes arrived on the
rawmidi input for the entire window.

This is not a driver-side failure to be debugged. `jockey3_stop_pcm_urbs()`
never touches the MIDI IN URB, its lock, or its stop fence -- confirmed by the
run's own dmesg, which contains nothing else at all for the whole window: no
MIDI IN submit error, no "cancelled without a driver-initiated stop", no
retry, nothing in `investigate` or `unexpected` (`dmesg: expected=3,
unexpected=0, unrelated=0, unclassified=0, investigate=0`). The MIDI IN URB
stayed submitted and healthy throughout and simply received nothing. That is
the firmware's own answer: **this device does not deliver MIDI IN while its
audio engine is not streaming.**

**Per the design stated at the top of this document, this closes the
feature.** A DJ controller whose jog wheels, faders and buttons go dead
whenever the PCM rings are idled down is a functional regression, not a
tradeoff, and no amount of refinement to the start/stop mechanism changes
what the firmware does with EP 0x83. The unique payoff on-demand streaming
could still have offered after the L0/power correction earlier in this
document -- the CPU and interrupt saving during genuine idle -- is not worth
that cost.

What this does *not* close: the STREAMING-bit experiment
("The second gate", above) still has independent value for
`re/ploytec_status_register.md` -- understanding the register is useful on its
own, restart-reliability work is unrelated to on-demand streaming, and that
document already scopes itself as not a dependency of any feature. It simply
no longer has an on-demand-streaming gate to unblock.

### 2026-08-29: OpenVizsla trace of the same test -- confirms the timing, and how the endpoint actually behaves

Rerun for the wire-level record, not to re-decide anything: `JT-MIDI-008`, run
`x86_64-prod/20260829T180442Z-functional`, same device and setup as above.
Identical result -- `deactivations_observed: 1`, `bytes_received_while_stopped:
0` -- with an OpenVizsla capture running throughout. Two independent hardware
runs now agree.

Capture `capture_2026-08-29_linux_test_on-demand.txt` (sidecar:
`re/usb/openvizsla/capture_2026-08-29_linux_test_on-demand.md`), parsed with
`re/usb/parse_openvizsla.py` and `reduce_transactions.py` per the pipeline in
`re/usb/README.md`. `extract_events.py` finds five control-plane events:

| # | kind | at (s) | span (ms) |
|---|---|---|---|
| 1 | enumeration | 2.030689 | 3.557 |
| 2 | init | 2.403042 | 2.093 |
| 3 | init+rate | 2.693785 | 82.620 |
| 4 | init | 38.540304 | 1.048 |
| 5 | init+rate | 38.823622 | 83.832 |

Events 1-3 are the case's opening rebind (unbind/bind, forcing the fresh probe
this document's design calls for); events 4-5 are its closing rebind. Nothing
else appears on EP0 between them.

**The timing correlates with the driver's own dev_dbg to within 1 ms, computed
from the trace's own clock alone -- no dmesg wall-clock conversion needed.**
Event 3 (the opening rebind's init+rate) ends at capture t = 2.693785 + 0.082620
= 2.776 s. The driver logged the deactivation as `idle for 5005 ms`. Predicted
stop: 2.776 + 5.005 = **7.781 s**. The last real PCM packet on the wire (a
bulk OUT on EP 0x05 immediately followed by two bulk IN on EP 0x86) is
timestamped **7.782163 s**. That is the on-demand deactivation, confirmed
independently of dmesg, not merely "around the right time."

Three MIDI IN completions on EP 0x83 follow immediately after (7.783872,
7.793258, 7.793264 s), each a 5-byte packet mostly `0xFD` padding with one or
two other bytes -- the device's ordinary idle heartbeat frames, filtered out
downstream by `jockey3_midi_in_callback()`'s `buf[i] < 0xF0` strip (see
`re/protocol_analysis.md` and `cases/midi_padding.py`), which is why they
never reach `bytes_received_while_stopped`. Then: **nothing at all on the bus
for 30.747 s**, until event 4's `SET_INTERFACE` at t = 38.540304 -- matching
`watch_seconds: 30` plus a little case bookkeeping.

**Ruled out: this is not USB autosuspend.** `jockey3_resume()` and
`jockey3_reset_resume()` both route through `jockey3_restore_device()`, which
is never silent on the wire -- it always runs a full cold init
(`SET_INTERFACE`, `GET_RATE`/`SET_RATE`, the closing `SET_STATUS`). A
runtime-PM suspend-then-resume inside the 30.747 s gap would show up as a
*third* `init`/`init+rate` event pair, distinct from the two rebinds. There
isn't one -- only five events, two rebinds, nothing between. (The driver also
never calls `usb_autopm_get_interface()`/`_put_interface()` and does not set
`.supports_autosuspend` on its `usb_driver` struct, consistent with this but
not the proof; the trace's event count is. To check the live policy on a rig
directly: `cat /sys/bus/usb/devices/<path>/power/{control,runtime_status,autosuspend_delay_ms,runtime_suspended_time}`
-- `<path>` is `1-13.1` for this device on this rig.)

**Resolved: the host kept polling EP 0x83 continuously throughout the gap;
every attempt was NAK'd.** Re-parsed from the raw capture with `--errors`
kept (the already-derived `_parsed.txt`/`_parsed_reduced.txt` files were both
generated without it and could not answer this after the fact -- exactly the
trap `re/usb/README.md` documents: *"with NAK and STALL dropped, an idle
window and a window full of rejected transfers are indistinguishable."*):

```
7.782478     | NAK        | 53.3     | 305   | [NAK x305 over 8.172 ms from 7.775679, resolved]
7.783872     | IN         | 53.3     | 5     | fd fd fd b1 f9
7.793258     | NAK        | 53.6     | 2     | [NAK x2 over 0.034 ms from 7.782489, abandoned]
7.793258     | IN         | 53.3     | 5     | fd fd fd 28 f9
7.793264     | IN         | 53.3     | 5     | fd fd fd 21 f9
7.893335     | NAK        | 53.3     | 4638  | [NAK x4638 over 99.990 ms from 7.793325, ongoing]
7.993344     | NAK        | 53.3     | 4636  | [NAK x4636 over 99.987 ms from 7.893336, ongoing]
8.093350     | NAK        | 53.3     | 4636  | [NAK x4636 over 99.986 ms from 7.993344, ongoing]
...
```

The last three real IN completions (the padding-byte packets already noted)
end at 7.793264; the tiny `abandoned` burst on `53.6` two lines up is the
last capture URB being killed. From 7.793325 -- essentially the same instant
-- `53.3` (EP 0x83) settles into an unbroken chain of `ongoing` NAK bursts,
~4636-4639 NAKs every ~100 ms with **sub-microsecond gaps between consecutive
burst windows**: verified over the first 1.9 s of the gap (88,103 NAKs
sampled), a rate of one attempt every 21.6 us. There is no mechanism by which
this would stop on its own before something explicitly kills the URB -- the
host is retrying a still-submitted bulk transfer, not waiting out a timeout --
and nothing does kill it until the closing rebind's `disconnect()`.

**This sharpens the finding rather than changing it.** The MIDI IN URB stays
submitted and the host polls it exactly as designed
(`jockey3_stop_pcm_urbs()` never touches `midi_in_urb`); the correct
statement of this document's central result is not "MIDI IN traffic stops"
but **"the pipe stays open; the firmware silently withholds while its audio
engine is idle."** It also puts a number on something asserted earlier in
this document only as a mechanism: "MIDI IN stays submitted... so the host
controller keeps polling EP 0x83 and the link does not leave L0" (see "What
the payoff actually is") is now measured, not just argued -- roughly 46,000
NAK'd attempts per second, sustained, is what "the link does not leave L0"
looks like on the wire.

## What happened to E3

`re/streaming_overhead_experiments.md` E3 specified a temporary,
development-only stop/start hook (a debugfs file or module parameter), a
three-rung escalation ladder -- bare resubmit, then `ploytec_start_streaming()`
only, then full `ploytec_initialize_device()` -- and a 20 ms stop rule, with the
MIDI IN question attached as a secondary check.

Most of that is superseded:

- **The escalation ladder is not needed.** The design goes straight to the top
  rung, a full cold init, as a deliberate choice rather than as a fallback. There
  is nothing to measure a ladder against.
- **The 20 ms budget is gone**, along with the stop rule built on it.
- **The throwaway hook is not built.** With `idle_timeout` writable at runtime,
  the real feature set to a few seconds *is* the experiment, so nothing has to be
  written that never ships.
- **The MIDI IN question survives intact**, promoted from a secondary check to
  the single gate.

E3's deliverable line, which called for the answer to be written back into
`re/streaming_overhead.md` Part 4, is replaced by this document.

## Scope and change impact

### The invariant being inverted

`jockey3.c:66-70` states the current contract:

> URBs run free for the lifetime of the device rather than being started and
> stopped around PCM use: the playback stream must keep flowing because it
> carries MIDI OUT, and the device expects a continuous packet stream. The PCM
> callbacks therefore only toggle whether a URB's payload is filled from (or
> copied to) an ALSA buffer.

That paragraph, the matching bullet in `CLAUDE.md`, and the notes and pass
criteria of several hardware test cases all encode it, and all have to change.

### New state

A `JOCKEY3_FLAG_STREAMING` bit in `chip->flags`, beside the existing
`JOCKEY3_FLAG_DISCONNECTED` and `JOCKEY3_FLAG_RESETTING` (`jockey3.c:263-265`),
records whether the PCM rings are *intended* to be running. It is a flag bit
rather than a `rate_mutex`-protected bool because it has to be readable from
contexts that do not hold the mutex -- most importantly
`jockey3_recover_urb_stream()`, which does its liveness check well before it
takes `rate_mutex`.

Alongside it: `last_activity` (an `atomic64_t` ktime, stamped from atomic
contexts), a `struct delayed_work idle_work` for deactivation, a
`struct work_struct activate_work` for activation requested from atomic context,
and a `pm_was_streaming` bool carried across suspend and reset.

The module parameter:

```c
static unsigned int idle_timeout = 600;
module_param(idle_timeout, uint, 0644);
MODULE_PARM_DESC(idle_timeout,
	"Seconds of inactivity before PCM streaming stops (0 = always stream).");
```

`0644` rather than the `0444` the three ALSA boilerplate parameters use, so the
value can be changed without reloading the module. `idle_timeout = 0` restores
today's continuous-streaming behavior exactly, which is both a user escape hatch
and the control condition for every measurement.

### Splitting the URB lifecycle

`jockey3_start_urbs()` and `jockey3_stop_urbs()` currently start and kill the
MIDI IN URB together with the two PCM rings. They split into a PCM pair and a
MIDI IN pair, with the existing combined entry points kept so that all twelve
current call sites are unaffected. Watchdog arm and disarm move into the PCM
pair, since the watchdog only ever watches the two PCM streams. The MIDI IN URB
is unanchored and fenced by `midi_stopping` under `midi_lock`, so its pair is
mechanically independent of the PCM anchors.

### Activation must fence MIDI IN

This is the least obvious consequence of the split, and it is a real trap.

`jockey3_set_rate()` calls `ploytec_initialize_device()`, which does an
unconditional `usb_set_interface(dev, 0, 1)` (`ploytec_proto.c:174`) and then
clears halts on each pipe. **EP 0x83 is on interface 0** (`jockey3.c:2965`), and
`usb_set_interface()` disables that interface's endpoints and unlinks every URB
on them.

Today this can never bite, because every `jockey3_set_rate()` caller is
sandwiched between `jockey3_stop_urbs()` and `jockey3_start_urbs()`, and both of
those handle the MIDI IN URB. Splitting the pair makes activation the first path
that would re-initialize the device with the MIDI IN URB still in flight. It
would come back `-ENOENT` or `-ESHUTDOWN` with `midi_stopping` clear, which is
exactly the condition the driver logs as "MIDI IN URB cancelled without a
driver-initiated stop" -- a `driver_fail` in the test framework's rules that no
`expect_dmesg` entry can whitelist -- and MIDI IN would be dead from the first
activation onward.

So activation is ordered stop-MIDI-IN, set-rate, start-PCM, start-MIDI-IN. The
honest statement of the feature is therefore that MIDI IN stays alive across the
whole idle period, but takes a brief gap on each activation -- not that it never
stops.

### Trigger points

**PCM start happens in `jockey3_pcm_hw_params()`, not in `.open`.** The obvious
hook is the open callback, but it would have to activate at the current rate,
and `hw_params` may immediately want a different one: an open at 48 kHz on a
device parked at 44.1 kHz would pay a full cold init at the wrong rate and then a
second one at the right rate. Two cold starts per open, both down the path that
failed roughly one change in six before `5505b28` and still shows a small
residual on `arm64-prod`, is the opposite of what this feature is for. Instead
the `current_rate == rate` early return at `jockey3.c:2529-2533` is gated on the
streaming flag: when the device is idle, control falls through to the
stop/set-rate/start sequence that already lives there, giving exactly one cold
start at the correct rate through code that is already hardware-proven, with the
existing post-change liveness-and-recover block validating it for free.

**MIDI OUT start goes through a work item.** `jockey3_midi_out_trigger()`
(`jockey3.c:2697`) runs with `midi_lock` held and interrupts disabled, so it
cannot start URBs itself; it stamps `last_activity` and queues `activate_work`.
No bytes are lost in the meantime. There is no driver-side MIDI FIFO -- the bytes
sit in the rawmidi core's own ring and are consumed by
`jockey3_get_next_midi_out_byte()` once packets start flowing again.

**Deactivation** happens from `idle_work`, and only when no PCM substream is
open in either direction, `midi_out_substream` is NULL, and `last_activity` is
older than `idle_timeout`. It needs no EP0 traffic at all -- the URBs are simply
killed.

### `probe()` does not change

`jockey3_initialize()` keeps its full initialization and URB start, and keeps
failing the probe when that start fails. That start is the driver's only proof
that the device can actually stream; removing it would let `probe()` succeed on a
device that cannot. Probe additionally arms `idle_work`, so a device that is
plugged in and never used drops to idle after `idle_timeout`. Ten minutes later
the observable behavior is identical to never having started, and the start path
gets exercised once per plug-in.

### Deliberate idle must not read as a stall

`jockey3_stop_urbs()` zeroes `last_callback_time`, and
`jockey3_check_urb_stream_alive()` returns false when it is zero -- so an
intentionally idle ring is indistinguishable from a stalled one. Every liveness
consumer is gated on the streaming flag: `jockey3_recover_urb_stream()` (which
can escalate all the way to `usb_reset_device()`), `jockey3_pcm_prepare()`'s
stall check, and the post-rate-change liveness block in `hw_params`.

The watchdog needs no change. `jockey3_stop_pcm_urbs()` disarms it, and
`jockey3_watchdog_check()` and `jockey3_watchdog_next_delay_ms()` already return
early on `stopping` and on `!started`.

Worth watching: `jockey3_recovery_budget_take()` is chip-wide and bounded at
three resets per sixty seconds. A start that fails on every open could exhaust
it, so the debug output has to make that visible.

### PM, reset and disconnect

Suspend records whether the device was streaming, stops everything as it does
today, and cancels both work items -- outside `rate_mutex`, because the driver's
locking hierarchy forbids a synchronous cancel under it. Resume initializes and
sets the rate as today, always restarts MIDI IN, and restarts the PCM rings only
if the device was streaming when it suspended. It then stamps `last_activity` and
re-arms the idle timer, so the device comes back in the same idle or active state
it went down in, with the inactivity clock restarted. Pre-reset and post-reset
follow the same save-and-restore.

Disconnect cancels both work items beside the existing watchdog cancel, before
stopping the URBs, and a devres action mirrors the watchdog's so that LIFO unwind
cancels the work before the URB stop runs. Both work items return early on
`JOCKEY3_FLAG_DISCONNECTED`.

### Observability

`dev_dbg()` on every state change: activation and which trigger caused it, the
measured start latency, deactivation, a failed activation, and an `idle_work`
tick that declined to stop and why. These lines are what the hardware cases
assert on.

### Test framework

The test framework classifies driver messages before it consults a case's
`expect_dmesg`, so a deliberate stop cannot whitelist its way out of a
`driver_fail` pattern; new lines need entries in `tests/hw/lib/rules.yaml`.
Writing `idle_timeout` needs root, so `tests/hw/priv/jockey3-testctl` gains a
verb, which means `priv/install.sh` has to be re-run on every target machine.
`idle_timeout` must not be passed via modprobe in a profile: an unknown module
parameter is itself classified as a driver failure, so any target still running
an older module would go red.

Several existing cases are written on the assumption being removed and need
their notes and pass text updated -- `JT-PERF-001` most of all, whose idle
measurement points assume a bound device with URBs running and nothing open, a
state that now expires.

## The second gate: can the STREAMING bit be cleared?

`ploytec_start_streaming()` reads the device's status byte and writes it back
with bit 5 (0x20) set. The obvious question this design never asks is the mirror
image: **what happens if the host clears that bit?** If the device then stops
sending, that is a graceful, protocol-level stop rather than the host simply
going silent -- and it would tell us what state the firmware believes it is in
while idle, which is currently unknown.

**First, a caveat about the name.** `PLOYTEC_STATUS_STREAMING` is not a
documented firmware constant. It was named from observation: writing the byte
with bit 5 set is what the vendors do at the end of every initialization and
every rate change, and doing it ourselves is what took the post-rate-change
capture stall from roughly one change in six to zero (`re/rate_change_stall.md`).
That establishes a correlation between the write and the device resuming, not
that bit 5 *is* an enable. The working assumption is that these `SET_STATUS`
calls set and clear bits in some internal Ploytec register whose mapping is
unknown -- the header already hedges by calling bits 0-4 "observed but not
understood", and the observed byte is 0x32, so at least two other bits are set
that nobody has explained.

So the experiment below should be read as "what does clearing bit 5 do", not as
"what does disabling streaming do". Establishing what the register actually
means is a separate and larger piece of work; it has its own document,
`re/ploytec_status_register.md`, and it is deliberately not a dependency of this
feature.

### What the corpus already says

Mined from `re/usb/openvizsla/*_events.txt`, no hardware needed:

- **177 `SET_STATUS` writes across the entire corpus. Every single one writes
  `wValue=0x0032`.** The bit is never cleared -- not once, by either vendor.
- Every `GET_STATUS` reply in the corpus is `0x32` as well, so the write is a
  read-modify-write that does not change the value -- in the captures the
  vendors never actually *change* the register, they just rewrite it. Our own
  `status | PLOYTEC_STATUS_STREAMING` writes 0x32 too, identical to the vendors.
  Clearing bit 5 means writing **0x12**, a value no host has ever been observed
  sending to this device.
- **Not even at teardown.** `capture_2026-08-13_macos_usb_disconnect_96k`
  contains nine `SET_STATUS` writes, all 0x32. macOS tears the device down
  without ever telling it to stop.

So there is no vendor-sanctioned stop sequence to copy. This is genuinely new
territory, which is what makes it worth probing -- and also what makes it risky:
writing an unobserved value to this firmware is the same class of action that
has wedged the device before (`re/playback_stall_wedge.md`).

### What it would and would not buy

**It would not, by itself, reduce bus traffic.** Wire activity on a bulk
endpoint is driven by the *host controller* issuing tokens, which it does for as
long as a URB is queued -- IN tokens on 0x86 and 0x83, PINGs on 0x05 -- and the
device's status bit cannot stop that. Clearing the bit with URBs still submitted
would leave the host polling and the device NAKing: the same traffic, or more of
it. Conversely, once the URBs *are* stopped the host has already ceased issuing
tokens for those endpoints, so there is nothing left for the bit to remove. The
residual traffic in this design is EP 0x83 polling, and that persists as long as
the MIDI IN URB is submitted, whatever the status bit says.

**What it plausibly buys is restart reliability**, and that is the reason to
run it. `re/rate_change_stall.md` established that this firmware has a
*latching* notion of streaming which the host must actively re-arm -- before
that was understood, capture failed to restart on roughly one rate change in
six. Stopping the URBs without telling the device leaves that latch set while
the host goes quiet, which is an undefined state we would be entering on every
idle period. Making the stop symmetric with the start is plausibly the
difference between an activation that works every time and one that does not.
Given that activation reliability is open question 3 below, that is worth real
effort.

Secondary: whatever the device's audio engine costs in power and heat while it
is streaming into a host that has stopped listening.

### Experiment design

Runs **after** `JT-MIDI-008`, and only if that passes. If the device stops
pumping EP 0x83 merely because the PCM URBs went away, it will certainly stop
when explicitly told to, and the feature is closed before this question matters.

Staged, stopping at the first failure:

1. **Does the write survive at all?** With everything streaming normally, write
   0x12, then `GET_STATUS`. Does the device accept it, does it report the bit
   cleared, and is it still enumerated afterwards? A stall or a wedge here ends
   the experiment.
2. **Does device-side traffic actually stop?** With the URBs still submitted,
   clear the bit and watch. This is the half that only an OpenVizsla trace can
   answer -- no ALSA counter reports wire activity, which is precisely the
   documented last-resort case for wire tracing. The expected observation is a
   transition from data packets to NAKs, *not* silence; if the trace shows the
   host still polling at the same rate, that confirms the mechanism above.
3. **Does MIDI IN survive it?** The same question `JT-MIDI-008` asks, under the
   stronger condition. If MIDI IN dies when the bit is cleared but survives a
   plain URB stop, then the bit is not usable in this design and the answer is
   simply "keep it set while idle".
4. **Does re-arming from a cleared bit restart cleanly**, and does it do so more
   reliably than a restart from a latch that was never cleared? This is the
   payoff, and it needs enough cycles to compare failure rates, not a single
   observation.

Every capture needs its metadata sidecar -- system and OS, driver version, and
the objective of the capture -- per `re/usb/README.md`.

## Plan

**Phase 0 -- reserve.** Claim the new test IDs in `tests/hw/catalog.yaml` as
`status: planned` on `main`, then rebase `dev/on-demand-streaming` onto it. Add
Milestone 17 to the workspace `implementation_plan.md`.

**Phase 1 -- the gate. Stop here if it fails.** Split the MIDI IN URB out of the
combined start/stop pair, add the minimal deactivate path and the `idle_timeout`
parameter. With a short timeout and nothing open, confirm on hardware that MIDI
IN still delivers while the PCM rings are stopped.

**Phase 2 -- the feature.** Everything described under "Scope and change impact".

**Phase 3 -- validation and write-up.** The remaining cases, the framework
changes, the updates to cases encoding the old invariant, and a measurement of
what idle actually costs now, written back into this document.

## Open questions, in the order worth attacking

**Question 1 is answered; the feature is closed. Questions 2-6 are moot for
on-demand streaming** and are kept below only as a record of what was never
reached, except question 2, which is redirected to
`re/ploytec_status_register.md` where it still has independent value.

1. ~~Does the device deliver MIDI IN while the PCM URBs are stopped?~~ **No.**
   Confirmed on hardware 2026-08-29 -- see "2026-08-29: the gate fails" above.
   The gate. Everything below was moot the moment this answer came back.
2. **Can bit 5 of the status register be cleared, and should it be?** No longer
   an on-demand-streaming question -- see `re/ploytec_status_register.md`,
   which absorbs it as a restart-reliability and register-understanding
   question in its own right, unblocked by anything here.
3. ~~How long does a cold-start activation actually take~~ -- moot, there is no
   activation to measure.
4. ~~How reliable is activation, over many cycles?~~ -- moot, same reason.
5. ~~What does idle actually cost after this change?~~ -- moot; idle now means
   "the feature does not exist", which is the `idle_timeout=0` baseline
   `re/streaming_overhead.md` already has.
6. ~~Does the idle timer ever fire in practice?~~ -- moot.
7. ~~Does EP 0x83 stay polled-and-NAK'd, or does polling stop entirely, while
   the PCM rings are down?~~ **Polled and NAK'd, continuously** -- ~46,000
   attempts/s, sub-microsecond gaps between burst windows, confirmed 2026-08-29.
   See "2026-08-29: OpenVizsla trace of the same test" above. Sharpens the
   central finding to "the pipe stays open; the firmware silently withholds"
   rather than changing it.

## Conclusion

**Not pursued further.** The gate specified at the outset of this document was
unconditional -- "if the answer is no, the feature is closed" -- and the
answer, on real hardware, is no. This is not a case for revisiting the design:
no amount of restart-reliability work, timeout tuning, or activation-latency
optimization changes what the firmware does with EP 0x83 while its audio
engine is idle, and that is the one fact the whole feature turned on.

Phase 1's driver code (`dev/on-demand-streaming`, `8352ff1`) is mechanically
sound -- the split URB lifecycle, the flag-as-ring-property fix, the idle
timer -- but it exists to serve a feature that is now closed. What happens to
the branch (revert, keep unmerged for reference, or salvage the URB-split
refactor on its own merits independent of on-demand streaming) is Frank's
call, not recorded here as a decision.

`re/streaming_overhead.md` Part 4 and `re/streaming_overhead_experiments.md`
E3 should both be updated to point at this conclusion rather than at an
open question.

## What this document does not claim

That on-demand streaming is a power-management feature. It is not -- see "What
the payoff actually is". That the study's recommendation against lever 3 was
wrong; the cost/benefit it describes is accepted, and the feature is being built
anyway, with the risk contained by a conservative timeout and a gate that can
still close it.
