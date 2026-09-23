# issue #49: wire-level analysis of the `set_rate_ep` failure

Status: two OpenVizsla captures of the actual failure, both analyzed in
detail. Source of record: github.com/fvdpol/alsa-jockey3/issues/49 (xref
#48, #50). Raw traces: `re/usb/openvizsla/capture_2026-09-23_linux_issue49_set_rate_stall_44k1.txt`
(filter_nak=true) and `...48k_nonak.txt` (filter_nak=false); their sidecars
carry the full capture metadata (device under test, module build-id,
trigger line). This document is the detailed technical analysis both
sidecars point to.

## Background

#49 is `JT-RATE-004` on i386-prod: a VBUS-only cut landing while
`issue49_vbus_cut_sweep.py --arm active` is rapidly reopening `aplay` at
rotating rates produces a reproducible `-71` (EPROTO) failure, dominantly
at `ploytec_set_rate()`'s first burst write (`set_rate_ep`). Host-side
`ISSUE48_TRACE()` instrumentation has shown, in every instance all
session, that everything up to and including `ploytec_get_rate()`
succeeds cleanly, and the failure is pinned to the very first `SET_RATE`
write immediately after. This investigation wires up OpenVizsla to see
what the device itself is doing at that exact point.

## Setup

Capture host `pi4test` (arm64-prod) running `ov_snapshot.py` under
**pypy3** (see `re/usb/ov-snapshot/ov_ftdi_capture_performance.md` --
pypy3 measured 0 packet overflow vs CPython's 51 on an identical
smoke-test burst, at roughly half the CPU; matters given `pi4test`'s
weaker single-thread performance than the i5-6500 the rest of that
document's numbers were measured on). DUT `alsa-test` (i386-prod) runs
`ov_snapshot_trigger.py`, tailing its own kernel log live and firing
`ov_snapshot.py`'s `/trigger` endpoint the instant a `set_rate_ep`-shaped
line appears. OV3 taps the bus between `alsa-test`'s USB host controller
and the hub the Jockey 3 sits behind, so the hub's own bus activity is
visible too.

Along the way: found and fixed a real bug in `tests/hw/priv/jockey3-testctl`'s
`kmsg-follow` (`dmesg --follow` fully block-buffers stdout once piped, so
a live reader can stall a long time between flushes -- fixed with
`stdbuf -oL`, commit 076e478). Settled on a small capture window
(`pre_seconds=1.0`/`post_seconds=2.0`): wide enough once the trigger reacts
to the live kernel line, and keeps the trigger-time verbose render pass
(the documented ~85%-of-a-core interpreter cost in
`ov_ftdi_capture_performance.md`) fast enough that retries don't compound
into a pileup, which a much wider window did cause once during setup.

## Capture 1: `filter_nak=true`, 44100 Hz

`issue49_vbus_cut_sweep.py --arm active --cycles 15`, cycle 1 hit
`set_rate_ep`. Trigger caught it clean: 60,553 packets, `overflow_delta=0`.

Kernel log (device number 9, `alsa-test`'s USB address 37):

```
54653.633  new high-speed USB device number 9        (reconnect after the VBUS cut)
54653.986  Reloop Jockey 3 Remix Firmware 0x31 v1.0.6 (GET_FIRMWARE succeeds)
   ... full init handshake, GET_RATE, all clean on the wire ...
54654.047  Failed to set rate on EP 0x86: -71
54654.048  probe with driver snd-reloop-jockey3 failed with error -71 (both interfaces)
54654.051  USB disconnect, device number 9             (+5.4ms after the failure)
54654.673  new high-speed USB device number 10          (+622ms after the disconnect)
```

On the wire (`re/usb/parse_openvizsla.py --errors=0` then
`re/usb/extract_events.py`; `--errors=0` reported **0 aggregated NAK/STALL
bursts, 0 unresolved** -- every extracted transaction completed cleanly by
the parser's own accounting):

```
GET_RATE ep=none wValue=0x0100 len=3 -> 44100 Hz          [clean, 135us]
  (14.569ms gap -- ordinary inter-request gap, same magnitude as capture 2)
SETUP: 37.0  DATA0: 22 01 00 01 86 00 03 00 [ACK]         <- SET_RATE(ep=0x86), wLength=3
  (41.4ms of nothing at all -- no OUT, no IN, no other traffic to addr 37)
PING: 37.0  x3 (4-8us apart) -- no response to any of them
SETUP: 37.0  DATA0: 01 0b 00 00 01 00 00 00  x6 retries   <- SET_INTERFACE(intf=1, alt=0)
  -- none of these six retries ever get a response either
  ... address 37 never appears in the trace again ...
IN: 50.1  [fresh enumeration begins at a new address]
```

The `SET_RATE(ep=0x86, wLength=3)` SETUP stage is sent and cleanly ACKed
by the device. Everything after that -- the OUT data phase that should
carry the 3-byte rate value, and the STATUS stage -- never happens. No
STALL, no NAK visible on this specific transfer (nothing at all, for
41ms, on that address). The device then disappears from address 37
entirely and reappears moments later at a new address (50): a full bus
reset.

## Capture 2: `filter_nak=false`, 48000 Hz -- ruling out a hidden NAK storm

`filter_nak=true` drops the PING/NAK handshake storm in gateware (see
`ov_ftdi_capture_performance.md`), which raised an obvious question:
was the device actually NAKing throughout that 41ms gap, with the filter
just hiding it? Reran with the filter off (accepted the cost: capture
host climbed to ~99% CPU and some overflow accumulated at idle, ~1400
events over several minutes, but every *triggered* capture still came
back `overflow_delta=0`).

Cycle 6 of a fresh 15-cycle sweep hit `set_rate_ep` again. Kernel log
(device number 21, address 49):

```
54801.341  new high-speed USB device number 21
54801.698  Reloop Jockey 3 Remix Firmware 0x31 v1.0.6
54801.759  Failed to set rate on EP 0x86: -71
54801.760  probe with driver snd-reloop-jockey3 failed with error -71 (both interfaces)
54801.766  USB disconnect, device number 21             (+7.3ms after the failure)
54802.385  new high-speed USB device number 22           (+619ms after the disconnect)
```

Identical wire shape, different address and rate:

```
GET_RATE ep=none wValue=0x0100 len=3 -> 48000 Hz          [clean, 135us]
  (14.264ms gap)
SETUP: 49.0  DATA0: 22 01 00 01 86 00 03 00 [ACK]
  (41.8ms of nothing -- filter is OFF this time, so any NAK would show)
PING: 49.0  x3 -- no response
SETUP: 49.0  DATA0: 01 0b 00 00 01 00 00 00  x6 retries -- no response
  ... reappears at address 50 ...
```

**With the filter off, there is still no NAK anywhere in that window.**
Checked directly that the tooling reliably captures handshake responses
when the device gives one: every *other* PING in this same trace gets an
ACK within 24us, without exception (several examples checked). So this
isn't a NAK storm the filter was hiding -- **the device goes genuinely
silent on the bus** for ~42ms right after accepting the SET_RATE SETUP
stage. Two independent captures, different device addresses, different
rates (44100 and 48000 Hz), identical shape: this looks like the real
mechanism, not a one-off.

## What was the last thing to happen before the silence?

The last *fully-completed* transaction in both captures is the preceding
`GET_RATE` query -- a clean round trip including its STATUS stage, with a
correct rate readback (capture 2's raw bytes: `80 bb 00` little-endian =
`0x00BB80` = 48000 decimal). A ~14ms gap follows, during which nothing at
all is attempted on the bus (looks like ordinary host-side driver
processing between the two calls, not device silence -- there's nothing
sent to blame the device for). Then the `SET_RATE(ep=0x86)` SETUP is sent
and ACKed -- that ACK is the actual last bus activity before the ~42ms
silence begins.

## Per-token timing: successful vs. failing `SET_RATE`

The same trace (capture 2) contains a fully successful `SET_RATE(ep=0x86)`
moments later, on the very next reconnect (device address 4), giving a
direct apples-to-apples comparison of normal turnaround times.

**Successful transaction** (address 4):

| step | delta from previous | absolute time |
|---|---|---|
| SETUP token | (14.6ms since prior transfer -- normal inter-request gap) | 1513.395361 |
| DATA0 (8-byte setup payload) | +0us | 1513.395361 |
| ACK (device accepts SETUP) | +1us | 1513.395362 |
| **PING** (host checks OUT readiness) | **+23us** | 1513.395385 |
| ACK (device says ready) | +1us | 1513.395385 |
| OUT token | +1us | 1513.395386 |
| DATA1 (3-byte rate payload) | +0us | 1513.395387 |
| NYET (device got it, briefly busy) | +1us | 1513.395387 |
| IN token (status stage) | +25us | 1513.395412 |
| DATA1 (zero-length status) | +1us | 1513.395413 |
| ACK (status accepted) | +0us | 1513.395413 |

Total SETUP-to-done: **52us**.

**The failing transaction** (address 49):

| step | delta from previous | absolute time |
|---|---|---|
| SETUP token | (14.264ms since prior transfer -- same normal gap) | 1512.331121 |
| DATA0 (byte-for-byte identical payload) | +0us | 1512.331121 |
| ACK (device accepts SETUP -- identical to the successful case) | +1us | 1512.331122 |
| **PING** | **+41,800us (41.8ms)** | 1512.372922 |
| PING (retry, no response to the first) | +4us | 1512.372925 |
| PING (retry, no response to the second) | +7us | 1512.372932 |
| SETUP (different request -- `SET_INTERFACE(1,0)`) | +903us | 1512.373835 |
| DATA0 | +0us | 1512.373836 |
| SETUP (retry, no response) | +4us | 1512.373840 |
| SETUP (retry, no response) | +4us | 1512.373844 |
| SETUP (retry, no response) | +1,143us | 1512.374988 |
| SETUP (retry, no response) | +4us | 1512.374992 |
| SETUP (retry, no response) | +4us | 1512.374996 |
| *(device gone -- reappears at a new address shortly after)* | | |

Every step through the SETUP's ACK is identical between the two --
same request, same payload, same 1us device turnaround. The divergence
is exactly one step later: **41.8ms where the successful case took
23us**, a ~1800x difference against a spec that gives a HS device at most
192 bit times (~400ns) to respond to a token. That gap is host-side
silence (nothing was transmitted during it, so it isn't the device's
non-response yet); only once the host does start probing (3 PINGs, then 6
retries of a different request) does the device's non-response become
visible and unambiguous.

## Did the bus itself stall, or just this endpoint?

Checked whether SOF (start-of-frame) tokens continued during the 41.8ms
gap -- if the host controller's own bus-timing heartbeat had stopped
(not just this one endpoint's scheduling), that would point at a very
different kind of problem, and would be long enough (USB 2.0's Device
Suspend Idle Time is 3.0ms) for the device to have legitimately
autosuspended and simply not be listening for ordinary tokens any more.

Literal `SOF` lines are not printed by `usb_interp.py`'s renderer
(`suppress = True` in its `pid == 0x5` branch, a declutter choice --
SOF arrives roughly every 125us and would swamp the log). But the
frame/microframe counter embedded in every printed line's
`[FRAME.SUBFRAME + offset]` bracket is populated directly from real,
captured SOF tokens (the same code decodes the SOF payload's frame number
field), not inferred from other traffic. Comparing it right at the
SET_RATE's ACK versus right at the first PING attempt:

```
1512.331122  [210.5 + 68.267]  ACK          <- frame 210, microframe 5
1512.372922  [252.4 +114.933]  PING : 49.0  <- frame 252, microframe 4
```

**210.5 -> 252.4 is 41.9 frames of advancement, matching the 41.8ms
elapsed almost exactly** -- a continuous count, no freeze, no jump. SOF
kept flowing at its normal cadence the entire time. This rules out a
bus-wide stall or the device having autosuspended: the bus's global clock
never missed a beat, and 41.8ms is nowhere near long enough on its own to
explain non-responsiveness via suspend when SOF never stopped. What
stalled was specifically the *next transaction for this one endpoint*,
not the host controller or the bus as a whole.

## Where this leaves the mechanism

- The device is demonstrably healthy and responsive right up through
  accepting the `SET_RATE(ep=0x86)` SETUP stage, in both captures.
- It then produces zero response to nine consecutive attempts (3 PINGs +
  6 SETUP retries) despite the bus itself staying live (SOF continuous).
- The 41.8ms gap before the host even attempts the first PING is itself
  ~1800x the normal turnaround and needs an explanation of its own --
  candidates not yet distinguished: the driver/xHC intentionally pausing
  before retrying (perhaps because something already signaled trouble),
  versus the same underlying stall already being visible in this gap from
  the host's side (e.g. an interrupt or completion the host controller
  itself was waiting on).

## Open follow-ups

1. Correlate this exact wire window against `ISSUE48_TRACE()`'s own
   kernel-side timestamps for this call, to see whether the 41.8ms gap
   is already present between the driver *submitting* the transfer and
   the host controller *issuing* the next token -- would tell us whether
   the delay originates in host software/scheduling or already reflects
   the same stall from the host controller's perspective.
2. A vendor (Windows/macOS) driver capture at the same transition, for
   comparison -- does the reference driver ever hit this window at all,
   or does its own timing/sequencing avoid it structurally?
3. Whatever explains the initial 41.8ms host-side gap likely also
   explains why repeated, tight reopen/rate-change churn is required to
   reproduce this (see the reap-delay position sweep, `re/rate_change_stall.md`
   open question 3) -- worth revisiting that finding once this gap is
   understood, rather than treating them as two separate open threads.
