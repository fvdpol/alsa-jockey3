# The Ploytec status register: we write it, and we do not know what it means

> **Status 2026-08-29: open, not started.** A standalone reverse-engineering
> topic, deliberately not a dependency of any feature. It was raised by the
> on-demand streaming work (`re/on-demand_streaming.md`) but its value is
> general: this register is on the critical path of every initialization and
> every rate change, and one bit of it is the difference between capture
> restarting and not.

## The problem

The driver reads and writes a one-byte device register over EP0:

```c
#define PLOYTEC_SET_STATUS		0x49	// bRequest to set device status
#define PLOYTEC_SET_STATUS_TYPE		0x40	// bmRequestType to set device status
#define PLOYTEC_REQ_STATUS		0x49	// bRequest to get device status
#define PLOYTEC_REQ_STATUS_TYPE		0xC0	// bmRequestType to get device status

/* Status Bits (bits 0-4 are observed but not understood, and unused) */
#define PLOYTEC_STATUS_STREAMING	0x20
```

The name `PLOYTEC_STATUS_STREAMING` is an inference, not a documented constant.
It was assigned because writing the byte back with bit 5 set is what both vendor
drivers do at the end of every initialization and every rate change, and because
making that write unconditional in this driver took the post-rate-change capture
stall from roughly one rate change in six to zero (`re/rate_change_stall.md`).

That is a strong correlation between the write and the device resuming. It is
not evidence that bit 5 *is* a streaming enable, and it says nothing at all
about the other bits. The working assumption is simply that these `SET_STATUS`
calls set and clear bits in some internal Ploytec firmware register, and that
what those bits map to is unknown.

## What is actually known

From `re/usb/openvizsla/*_events.txt`, the whole capture corpus:

- **Every `GET_STATUS` reply is `0x32`.** Not 0x20 -- so bits 1 (0x02) and 4
  (0x10) are also set on a normally operating device, and neither has any
  explanation.
- **All 177 `SET_STATUS` writes carry `wValue=0x0032`.** Every one, both vendors,
  every capture. The vendors never change the register's value; they rewrite it
  with the value they just read.
- **The bit is never cleared, not even at teardown.**
  `capture_2026-08-13_macos_usb_disconnect_96k` contains nine `SET_STATUS`
  writes, all 0x32. macOS disconnects the device without ever telling it to stop.
- **The write does something beyond setting a bit.** This is the load-bearing
  observation from `re/rate_change_stall.md`: the device already reports the bit
  set when the write is issued, so a conditional write would never be sent at
  all -- and yet issuing it unconditionally is what fixed the stall. Whatever
  `SET_STATUS` does, it is not merely "store this byte".

That last point is the reason this register deserves its own investigation. A
write that is a no-op by its apparent semantics, but is required for correct
behavior, means the apparent semantics are wrong.

## Two ways to attack it, and they are complementary

### 1. Behavioral: drive the bits and watch

Write values other than 0x32 and observe what changes, in three places at once:
the device's audible and visible behavior, its subsequent `GET_STATUS` replies,
and the USB bus. The bus half needs an OpenVizsla trace -- no ALSA counter
reports wire activity, which is exactly the documented last-resort case for wire
tracing (`re/usb/README.md`).

Points worth being careful about:

- **These are unobserved values.** No host has ever been seen writing anything
  but 0x32. The firmware has a documented history of wedging
  (`re/playback_stall_wedge.md`), so escalate one bit at a time from a known-good
  state, and capture the device's diagnostic state *before* power-cycling
  anything that hangs.
- **Read back after every write.** Whether a written bit sticks, silently
  reverts, or reads back differently from what was written is itself a result.
- **Some bits may be read-only status rather than control.** The register is
  addressed by one request number in both directions, which does not mean the
  same bits mean the same thing in each.

### 2. Static: the vendor binaries in Ghidra

The vendor Windows driver is in the workspace under `Windows Driver/`:
`rlj3rmxa.sys`, `rlj3rmxm.sys`, `rlj3rmxu.sys`, plus `rlj3rmx_x64.dll` and
`rlj3rmx_x86.dll`. Finding the code that constructs a `bRequest = 0x49` control
transfer should lead directly to whatever symbol, constant or bitfield the
vendor uses for the value, and that is likely to be more informative in an
afternoon than a week of black-box probing.

This is reading a competitor's binary to understand a hardware protocol, not to
copy an implementation. **The project's licensing position must not be put at
risk:** the driver is sole-authored, GPL-2.0-or-later, with no code copied from
any other implementation. Findings from this work are recorded as protocol
facts -- request numbers, bit meanings, sequences -- and never as transcribed
code or transcribed structure.

## Why it is worth doing at all

Everything this driver knows about the device's control plane was learned by
watching what the vendors do and copying it. That has worked, but it means the
driver's reliability rests on imitation rather than understanding, and the
places where imitation has proven insufficient are exactly the places that have
cost the most time: the rate-change capture stall, the wedge, the cold-boot
race. A real map of this register would convert one of those unknowns into
something that can be reasoned about.

Concretely, it would answer questions the driver currently cannot:

- Is there a graceful "stop" the device understands, as opposed to the host
  simply going quiet? (Asked, for its own reasons, by
  `re/on-demand_streaming.md`.)
- What do bits 1 and 4 report, and does the driver ever need to look at them?
- Does the register expose an error or fault state that would give the watchdog
  something better to test than "no completions for 20 ms"?
- What makes the unconditional write necessary, and is there a cheaper or more
  reliable way to achieve the same thing?

## Scope

Explicitly **not** a dependency of on-demand streaming or of any other feature
in flight. No driver behavior changes on the strength of this work without its
own validation. The deliverable is knowledge written into this document, and
`ploytec_proto.h`'s comments corrected to match whatever is actually established.

## Open questions, in the order worth attacking

1. **What does the vendor binary call this register and its bits?** Cheapest
   first step by a wide margin, and it needs no hardware.
2. **What does the device do when bit 5 is cleared?** The concrete question
   already queued behind on-demand streaming's own gate.
3. **What are bits 1 and 4?** Set on every healthy device, never explained.
4. **Why is the unconditional write load-bearing** when its apparent effect is a
   no-op? The most interesting question here, and probably the one whose answer
   changes the driver most.
5. **Are bits 0, 2, 3, 6 and 7 ever set on any device?** All observations so far
   come from two units of one model; the Master Edition may differ.
