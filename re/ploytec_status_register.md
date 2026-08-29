# The Ploytec status register: we write it, and we do not know what it means

> **Status 2026-08-29: static analysis done (Windows PE + symboled macOS
> kext); behavioral probing not started.** A standalone reverse-engineering
> topic, deliberately not a dependency of any feature. It was raised by the
> on-demand streaming work (`re/on-demand_streaming.md`) but its value is
> general: this byte is written unconditionally on the critical path of every
> stream start and every rate change. The two "What the vendor ... shows"
> sections below are the settled account: `bRequest = 0x49` is a generic,
> `wIndex`-dispatched device-configuration request, and the driver's "status"
> byte is what the vendor calls `PGDevice::setAJDMAInputChannels`. What is
> still unknown is the *device's* response to bit 5 -- that needs attack #1.

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
  every capture. On the wire the vendors never change the register's value.
  *This is a fact about the trace, and it stays true.* What the trace cannot
  show -- and what the static analysis below does show -- is that the vendor
  code does not echo the byte it read; it recomputes it, and on a healthy
  Jockey 3 the recomputed byte happens to equal 0x32 every time.
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

## What the vendor Windows binary shows (static analysis, 2026-08-29)

> **Superseded in part by "What the vendor macOS kext shows" below.** The macOS
> kext carries full C++ symbol names, and it corrects two guesses made here
> from the stripped Windows binary: the `wIndex = 1`, `value |= 0xe7` path seen
> in `rlj3rmxu.sys` is *not* a non-Jockey vendor -- it is `setEsuCpldByte`,
> which is real code that is simply gated off for the Jockey 3; and the
> "streaming / rate setup path" write is the function the macOS driver calls
> `setAJDMAInputChannels`. Read the macOS section for the settled account; this
> section is kept for the wire-shape confirmation and the Ghidra addresses.

Static disassembly of `Windows Driver/rlj3rmxu.sys` (the Ploytec core USB
driver, 64-bit). The binary is a stripped release PE with no symbols, so these
are protocol facts read from call-graph position and the retained debug format
strings -- not vendor names for the register or its bits. No code or structure
is transcribed. Tooling was `objdump` plus manual PE section-offset arithmetic;
Ghidra was not used or needed for this pass. Navigation addresses for a Ghidra
session are collected at the end of this section.

### The transport is generic, and matches the driver's constants

Every `bRequest = 0x49` transfer, both directions, is built by the one EP0
vendor/class request helper that every Ploytec vendor request in this binary
goes through. That helper takes the direction, the request class
(vendor vs. class), and the recipient (device / interface / endpoint) as
separate arguments and assembles the Windows URB from them; there is no
`bmRequestType` byte in the vendor's source form. For all `0x49` call sites the
request class is "vendor" and the recipient is "device", so the transfers on
the wire are exactly:

- **GET_STATUS** -- `bmRequestType = 0xC0`, `bRequest = 0x49`, `wValue = 0`,
  `wIndex = 0`, one data byte IN.
- **SET_STATUS** -- `bmRequestType = 0x40`, `bRequest = 0x49`, `wValue =` the
  status byte, `wIndex = 0`, no data stage.

Both match `PLOYTEC_SET_STATUS` / `PLOYTEC_REQ_STATUS` and their
`bmRequestType` values in `ploytec_proto.h` with nothing left over. The
retry count the vendor uses on these transfers is 5.

Some **non-Jockey** device paths in the same multi-vendor binary issue
`bRequest = 0x49` with `wIndex = 1` or `wIndex = 2`. The status request is
shared across Ploytec devices and `wIndex` is a sub-selector on some of them;
on the Jockey 3 paths it is always 0.

### The written value is computed bit-by-bit, never echoed

Every `SET_STATUS` in the binary is preceded by a `GET_STATUS` whose result is
copied and then patched one bit at a time. The patch differs by code path:

- **A dedicated streaming-enable helper** (called once, from inside the
  bulk-audio-out / start path) takes an "enable" argument. When it means
  *on* it sets bit 5 (`0x20`); when it means *off* it **clears bit 5**. It
  then always sets bit 4 (`0x10`), conditionally sets bit 5 again from the
  result of a codec-register probe, and writes the byte back
  **unconditionally** -- there is no compare-against-the-old-value guard on
  this path.
- **The streaming / rate setup path** does: `GET_STATUS`; if that transfer
  failed, issue no write at all; otherwise copy the byte, clear bit 1
  (`0x02`) and then conditionally set it again from an unresolved runtime
  check, set bit 4 (`0x10`), clear bit 3 (`0x08`), and write back **only if
  the resulting byte differs** from what was just read.
- **A rate-related path** ORs bit 5 (`0x20`) into the byte before writing.

### Why the trace only ever shows 0x32

A healthy Jockey 3 reads `0x32` = bits 1, 4 and 5 set; bits 0, 2, 3, 6, 7
clear. The setup path's patch -- set bit 4, clear bit 3, keep bit 1 -- leaves
`0x32` unchanged, so its "write only if changed" guard suppresses the write.
That is why all 177 captured `SET_STATUS` writes carry `0x0032` and none carry
anything else: the captures are dominated by the guarded path on a device that
is already in the target state. The unguarded helper does still fire, and it
too computes `0x32` on a healthy unit.

### Bit meanings, as far as this supports them

| Bit | Value | What the vendor code does with it | Confidence |
|---|---|---|---|
| 5 | 0x20 | Streaming enable. A dedicated helper sets it for "start" and **clears it for "stop"**; a rate path ORs it in. | Toggled both ways by the vendor. Supports the `PLOYTEC_STATUS_STREAMING` name as a *control* bit. The device's actual response to a cleared bit has still not been observed. |
| 4 | 0x10 | Set in every branch of every `SET_STATUS` path examined. | Always set. Reads like an "apply / valid" flag or is simply required. Meaning unknown. |
| 3 | 0x08 | Cleared in the setup path. | Meaning unknown. |
| 1 | 0x02 | Set or cleared from an unresolved runtime check, in two different paths. Not a static bit. | Mechanism known, meaning unknown. |
| 0, 2, 6, 7 | -- | Not touched by any `0x49` path found. | Consistent with "never seen set". |

### What this says about the unconditional write (open question 4)

The vendor has **both** shapes: a guarded "write only if the byte changed"
path and an unguarded "always write" helper. The Linux driver's unconditional
`SET_STATUS` therefore matches an existing vendor code path rather than
diverging from vendor behavior. The static read still does not explain *why*
the write re-arms capture when the byte is unchanged -- but the fact that the
vendor keeps an unconditional path at all is consistent with the working
hypothesis that the device acts on *receiving the request*, independent of the
value carried.

### Ghidra navigation aids

Image base `0xf1000000`. `.text` VMA = file offset + `0xf1000c00`; `.rdata`
VMA = file offset + `0xf1001a00` (use this to line up string cross-references
with the addresses below).

- **EP0 vendor/class request helper:** `0xf1042b30`. Writes the URB
  `Request` / `Value` / `Index` fields at URB offsets `+0x81` / `+0x82` /
  `+0x84`; direction-IN when its 2nd argument is `0x80`; request class from
  the 3rd argument (`0x40` vendor, `0x20` class); recipient from the 4th
  argument (`0`..`3`).
- **Streaming-enable helper, unconditional `SET_STATUS`:** `0xf101cee0`, with
  its single caller at `0xf101c089` (inside a function that references the
  `PGDevice::bulkAudioOut()` debug strings).
- **`SET_STATUS` in the streaming / rate setup path, guarded:** the sequence
  around `0xf1035050`.
- Unresolved for a Ghidra pass: the runtime check that gates bit 1 (called at
  `0xf105d9a0` from the setup path), and confirmation that there is no fourth
  `SET_STATUS` value shape.

## What the vendor macOS kext shows (symboled, 2026-08-29)

The macOS driver was found as a Mach-O x86-64 kext (`RL_J3_REMIX`, in the
project's Ghidra workspace). Unlike the Windows PE it **retains full C++ symbol
names with parameter type lists** -- roughly 1600 symbols -- so this is the
authoritative account of the three earlier questions. Tooling: `llvm-nm`,
`capstone`. Still no code or struct layout transcribed; what is recorded below
is request shapes, the vendor's own function names, and per-bit operations.

### The request is one generic vendor request, dispatched by `wIndex`

Every `bRequest = 0x49` transfer in the kext goes through one method whose
demangled prototype is:

```
PGKernelDeviceDrvRLJ3Remix::deviceRequest(EDirection, EDRQ_Level,
    EDRQ_Recipient, unsigned char bRequest, unsigned short wValue,
    unsigned short wIndex, void *buffer, unsigned int &length, bool, int retries)
```

That confirms the argument order guessed from Windows exactly. `EDirection` is
`0x80` for IN and `0` for OUT; `EDRQ_Level` is `0x40` for a vendor request;
`EDRQ_Recipient` is `0` for device. So the wire shapes are `bmRequestType =
0xC0` (GET) and `0x40` (SET), `bRequest = 0x49` -- matching `ploytec_proto.h`
-- and the retry count is 5, as on Windows.

`bRequest = 0x49` is **not** a status register in the vendor's model. It is a
generic device-configuration request, and the *sub-register is selected by
`wIndex`*. The call sites, by vendor function name:

| Caller (vendor name) | `wIndex` | Length | What it configures |
|---|---|---|---|
| `PGDevice::setAJDMAInputChannels(unsigned int)` | 0 | 1 | **This is the driver's "status" byte.** |
| `PGDevice::updateAjInputSelector(unsigned int *)` | 0 | 2 | Input source selector; polls a bit (see below). |
| `PGDevice::writeDigitalOutSelector()` | 2 | 2 | S/PDIF vs. AES / digital-out routing. |
| `PGDevice::setEsuCpldByte(unsigned char)` | 1 | 0 | Named for a CPLD on some *other* Ploytec/ESI product; gated by VID/PID, never runs on a Jockey 3, which has no CPLD on the board. Ignore for this driver. |
| `PGDevice::readUS3XXChannelConfig()` | -- | -- | Tascam US-3xx only. |

"AJ" / "Aj" is the vendor's identifier for the Jockey 3 device family
throughout the kext (`setAJDMAInputChannels`, `updateAjInputSelector`,
`ajHasNoDigital`, `AJ::streamingStarted`).

### `setAJDMAInputChannels` is the driver's SET_STATUS / GET_STATUS

It is called once, from `PGDevice::bulkAudioRun()` -- the routine that spins up
bulk audio streaming -- immediately after a 50 ms sleep, on every stream start
and every rate change. It:

1. Issues `GET_STATUS` (`wValue = 0`, `wIndex = 0`, IN, one byte).
2. Computes two candidates from the byte just read: `read | 0x20` and
   `read & ~0x20`.
3. Picks `read & ~0x20` -- i.e. **clears bit 5** -- when its `unsigned int`
   argument is `>= 17`, otherwise picks `read | 0x20` (**sets bit 5**). The
   argument comes from a field of the current stream settings.
4. Unconditionally sets bit 4 (`| 0x10`).
5. Issues `SET_STATUS` with that byte (`wIndex = 0`, OUT, no data stage).
6. Returns `0x20` or `0x10` to `bulkAudioRun`, which uses it to choose the
   capture bulk-IN packet geometry (a 4-channel vs. 8-channel layout).

There is **no compare-and-skip** here: the GET and the SET both happen every
time `bulkAudioRun` runs. On a healthy Jockey 3 the read is `0x32`, step 3
keeps bit 5, step 4 keeps bit 4, and the byte written back is `0x32`
unchanged -- which is why the whole capture corpus shows `wValue = 0x0032` and
nothing else, even though the write is real and unconditional.

### Bit meanings -- settled account

| Bit | Value | Evidence | Reading |
|---|---|---|---|
| 5 | 0x20 | `setAJDMAInputChannels` sets it for the `< 17` case and **clears it for the `>= 17` case**; its return value then selects the capture packet layout. | A real control bit that gates or sizes the capture DMA path. `PLOYTEC_STATUS_STREAMING` is a defensible name. There *is* a vendor code path that clears it -- answers open question 2 for the request shape, though not the device's response. |
| 4 | 0x10 | `\| 0x10` unconditionally before every `SET_STATUS` in `setAJDMAInputChannels`. | An "apply" / "valid" bit, or simply required set. Meaning still unknown. |
| 2 | 0x04 | `updateAjInputSelector` reads the byte, and if bit 2 is set, sleeps 20 ms and re-reads before proceeding. | A **busy / pending** status bit. First evidence that any bit is *read* as meaningful state. Not exercised on the `setAJDMAInputChannels` path. |
| 3 | 0x08 | Part of a `0x18` two-bit field composed in `updateAjInputSelector`; cleared in the Windows digital-out path. | Selector / routing state, not streaming. |
| 1 | 0x02 | Set on a healthy device (`0x32`); `setAJDMAInputChannels` does not touch it; `updateAjInputSelector` preserves it through a `& 0xe7`. | Unknown; carried, not controlled, on the Jockey path. |
| 0, 6, 7 | -- | Never set by any `0x49` path in either binary. | Consistent with "never observed set". |

### What this settles about the unconditional write (open question 4)

`setAJDMAInputChannels` -- the vendor's own code, on the Jockey 3 path -- does
an **unconditional GET then SET** of this byte on every `bulkAudioRun`, i.e.
every stream start and every rate change. The Linux driver making its write
unconditional is not a workaround that diverges from the vendor; it is what the
vendor does. The name (`...DMAInputChannels`) and the use of the return value
to pick the capture packet layout both point the same way: writing this byte
**(re)configures the capture DMA engine**, which is a concrete mechanism for
why re-issuing it clears the post-rate-change capture stall
(`re/rate_change_stall.md`) even when the byte value does not change.

### Ghidra navigation aids (macOS kext)

Mach-O, single arch, file offset == virtual address. `__text` starts at file
offset `0xea8`, `__cstring` at `0x47711`. Calling convention is System V
(args in `rdi, rsi, rdx, rcx, r8, r9`, then stack).

- `PGKernelDeviceDrvRLJ3Remix::deviceRequest(...)` @ `0x1cd3e` -- the demangled
  prototype gives the whole argument layout.
- `PGDevice::setAJDMAInputChannels(unsigned int)` @ `0x60c0` -- the driver's
  GET_STATUS + modify + SET_STATUS.
- `PGDevice::bulkAudioRun()` @ `0x632c` -- its only caller; 50 ms sleep, then
  the call, then the return value picks the capture packet geometry.
- `PGDevice::updateAjInputSelector(unsigned int *)` @ `0x1005c` -- same request,
  polls bit 2 (`0x04`) as busy.
- `PGDevice::writeDigitalOutSelector()` @ `0x10c08` -- same request, `wIndex 2`.
- `PGDevice::setEsuCpldByte(unsigned char)` @ `0x10d6c` -- same request,
  `wIndex 1`, `value |= 0xe7`, gated by VID/PID to an ESI product. Not the
  Jockey 3 (no CPLD on that board -- confirmed against the schematic). Listed
  only so the `wIndex 1` traffic in the Windows binary is accounted for.

### Where the byte physically lands on the Jockey 3

The Jockey 3 has no CPLD. Its USB side is an **ISP1583** device controller
driven by an **ATmega8515**; a separate **STM8S207** runs the control surface
and a **DSP56374** does stand-alone mixing (see `re/jockey3_hardware.md`). The
`bRequest 0x49` / `wIndex 0` byte is handled by the ATmega8515 firmware and, by
its name and effect (`...DMAInputChannels`, return value sizes the capture
packet), takes effect in the ISP1583 bulk-IN DMA setup. This is also a caution
about the vendor binary generally: it is one library covering a large VID/PID
list (Elektron, Allen & Heath, Tascam, ESI, ...), so a vendor symbol like
`setEsuCpldByte` or `XeUSBPrePicCommand` names a chip or feature on *some*
Ploytec product, not necessarily anything on the Jockey 3 board.

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

### 2. Static: the vendor binaries

**Two passes are done -- see the two "What the vendor ... shows" sections
above.** The stripped Windows PE (`rlj3rmxu.sys`) confirmed the wire shapes;
the symboled macOS kext (`RL_J3_REMIX`) then named everything: the request is
generic, `wIndex`-dispatched, and the driver's "status" byte is
`PGDevice::setAJDMAInputChannels`, called unconditionally from
`bulkAudioRun()`. What is *still* unresolved: the device's actual response to
bit 5 being cleared (needs the behavioral pass), and what bits 1, 3 and 4 mean
physically.

The macOS kext is the better artifact for any further static work -- full C++
symbols. The Windows `x86` ASIO DLL (`rlj3rmx_x86.dll`) is a possible third
source; `rlj3rmxa.sys` (WDM audio adapter) and `rlj3rmxm.sys` (MIDI shim) do
not touch EP0 and can be skipped.

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

The two "What the vendor ... shows" sections have grown past the status
register itself -- they now hold general findings about the vendor binaries
(the shared multi-device library, the `wIndex`-dispatched `0x49` request
family, the macOS kext symbol map, Ghidra navigation addresses). If a broader
static-analysis effort starts, carve those sections out into their own
`re/vendor_binary_analysis.md` and leave this document with just the
status-register-specific conclusions and a cross-reference.

## Open questions, in the order worth attacking

1. ~~**What does the vendor binary call this register and its bits?**~~
   **Answered (2026-08-29), from the macOS kext symbols.** `bRequest = 0x49` is
   a generic device-configuration request dispatched by `wIndex`; the driver's
   byte is `wIndex = 0`, and the vendor function that reads-modifies-writes it
   is `PGDevice::setAJDMAInputChannels`, called unconditionally from
   `bulkAudioRun()`. See "What the vendor macOS kext shows".
2. **What does the device do when bit 5 is cleared?** The request shape for a
   clear is fully known now (`setAJDMAInputChannels` with its argument `>= 17`),
   but the device's *response* has still not been observed. This is now purely
   a behavioral-pass question -- still the one queued behind on-demand
   streaming's gate.
3. ~~**What are bits 1, 3 and 4?**~~ *Mechanisms known, physical meaning open.*
   Bit 4 is set unconditionally before every write; bit 3 is a selector/routing
   bit (part of a `0x18` field in `updateAjInputSelector`); bit 1 is carried
   but not controlled on the Jockey path; **bit 2 is a busy/pending status bit**
   the vendor polls on. See the bit table in the macOS section.
4. ~~**Why is the unconditional write load-bearing?**~~ **Answered.**
   `setAJDMAInputChannels` does an unconditional GET+SET on every `bulkAudioRun`
   (every stream start, every rate change); the Linux driver's unconditional
   write matches it. The vendor name and the return-value use both say the
   write **(re)configures the capture DMA path**, which is a concrete mechanism
   for the rate-change stall fix.
5. **Are bits 0, 6 and 7 ever set on any device?** Never set by any `0x49` path
   in either binary. All wire observations are still from two units of one
   model; the Master Edition may differ.
