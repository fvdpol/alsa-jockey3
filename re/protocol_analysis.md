# Reloop Jockey 3 Remix USB Protocol Analysis

## Overview
The Reloop Jockey 3 Remix uses a proprietary USB 2.0 protocol for audio and MIDI. It is a **Ploytec GmbH** based device using the **"usb_stream"** protocol. It is not USB Class Compliant.

## Endpoints
| Endpoint | Type | Direction | Max Packet | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| 0x02 | Isochronous | OUT | 4 | Clock Synchronization / Timing |
| 0x05 | Bulk | OUT | 512 | Audio Playback (4-ch, 24-bit) + MIDI OUT (LEDs) |
| 0x86 | Bulk | IN | 512 | Audio Capture (4/6-ch, 24-bit) |
| 0x83 | Bulk | IN | 512 | MIDI IN (Controls) |

## Control Transfers (Initialization)
The device follows the standard Ploytec handshake:
1. **Wake-up**: 
   - `c0 56 ...` (Request 0x56) -> Read 15 bytes (Firmware Version).
   - `c0 49 ...` (Request 0x49) -> Read 1 byte (Status).
2. **Set Alternate Setting**:
   - Interface 0 -> Alt 1
   - Interface 1 -> Alt 1
3. **Sample Rate**:
   - `22 01 00 01 86 00 03 00` (SET_CUR on EP 0x86) -> 3-byte LE rate (e.g., `44 ac 00` for 44.1k).
   - `22 01 00 01 05 00 03 00` (SET_CUR on EP 0x05) -> 3-byte LE rate.
4. **Confirm Status**:
   - Write back the status byte read in step 1 with bit 5 set (Request 0x49, bmRequestType 0x40).
   - The observed byte is `0x32`, so bits 1 and 4 are set as well and are not
     understood. Bit 5 is called "streaming" in the driver on the strength of
     observation alone, not of any documentation. What this register actually
     maps to is an open reverse-engineering topic --
     see `re/ploytec_status_register.md`.

## Audio/MIDI Packet Layout (EP 0x05 OUT / 0x86 IN)

The device uses two different standard Ploytec layouts for output and input streams:

### Playback Endpoint (EP 0x05 OUT)
Each 512-byte sub-packet contains:
- **0 - 479**: Audio Payload (480 bytes).
  - **10 frames** of bit-interleaved audio.
  - Each frame = **48 bytes**.
  - Format: S24_3LE bit-interleaved.
  - Split into Odd channels (`bytes 0-23`) and Even channels (`bytes 24-47`).
- **480**: MIDI Byte.
  - `0xFD` = Idle (no MIDI data).
  - Any other value = Raw MIDI byte.
- **481**: Sync Byte (`0xFF`).
- **482 - 511**: Padding. Must be **Zero-filled** (`0x00`).
  - *Capture Analysis*: Filling this gap with `0xFD` spams the device's internal parser and causes buffer overflows/truncation.

**MIDI Timing & Protocol Constraints**:
- **Rate Limit**: While the official Windows driver uses a conservative ~500 bytes/sec, the hardware reliably supports up to **~3200 bytes/sec** (approximately one byte per USB sub-packet at 44.1/48kHz). Rates above 3500 bytes/sec cause erratic behavior and buffer overflows. The Linux driver settles on the standard MIDI rate of **3125 bytes/sec** for optimal responsiveness and stability.
- **Encapsulation**: Exactly one MIDI byte per 512-byte sub-packet.
- **Running Status**: **NOT SUPPORTED** by hardware. Every MIDI message must include its status byte (e.g., `0x90`). The Linux driver implements a "Running Status Expander" to ensure compatibility with standard ALSA MIDI streams.

### Capture Endpoint (EP 0x86 IN)
Each 512-byte sub-packet contains:
- **0 - 511**: Audio Payload (512 bytes).
  - **8 frames** of bit-interleaved audio.
  - Each frame = **64 bytes**.
  - Format: S24_3LE bit-interleaved.
  - Split into Odd channels (`bytes 0-23`) and Even channels (`bytes 32-55`). No MIDI byte is multiplexed on this endpoint (MIDI IN is handled entirely by EP `0x83`).

## Codec Logic (8-channel Ploytec mapping to 4/6 physical channels)

The Ploytec codec scatters the bits of the 24-bit samples across 24 bytes of the USB stream.
For any active channel, bit `k` of byte `slice_offset + n` (0 <= n < 24) maps to bit `23 - n` of the sample.

### Playback Encoding (4 physical channels mapped to 8-channel layout)
- **First 24 bytes (Odd channels):**
  - Bit 0: Bit `(23-n)` of ALSA Channel 1 (Master Left).
  - Bit 1: Bit `(23-n)` of ALSA Channel 3 (Headphone Left).
  - Bit 2–3: Zero (unused).
- **Second 24 bytes (Even channels):**
  - Bit 0: Bit `(23-n)` of ALSA Channel 2 (Master Right).
  - Bit 1: Bit `(23-n)` of ALSA Channel 4 (Headphone Right).
  - Bit 2–3: Zero (unused).

### Capture Decoding (8-channel layout mapped to 6 physical channels)
- **First 24 bytes (Odd channels, bytes 0–23):**
  - Bit 0: Bit `(23-n)` of ALSA Channel 1 (Input 1 Left).
  - Bit 1: Bit `(23-n)` of ALSA Channel 3 (Input 2 Left).
  - Bit 2: Bit `(23-n)` of ALSA Channel 5 (Microphone).
  - Bit 3: Unused.
- **Second 24 bytes (Even channels, bytes 32–55):**
  - Bit 0: Bit `(23-n)` of ALSA Channel 2 (Input 1 Right).
  - Bit 1: Bit `(23-n)` of ALSA Channel 4 (Input 2 Right).
  - Bit 2: Bit `(23-n)` of ALSA Channel 6 (Microphone).
  - Bit 3: Unused.

note that the microphone input has the output of the analog front-end connected to both the L and R inputs for the PCM 1803A ADC; so both channels will carry the same (mono) signal.

### Why the format looks like this (hardware origin)

The "bit-interleaved" capture format is almost certainly not a codec transform
at all -- it is the raw parallel-sampled state of the ISP1583's DMA data bus.
See `re/jockey3_hardware.md`: the USB controller runs in Split bus mode, and
the ADC serial-data lines `SDI0..SDI2` are wired straight onto the 16-bit DMA
bus `DATA[15:0]` (defined in the schematic as `SDI15..SDI0`).

Read a capture frame as: **byte index = one bit-clock tick, bit position
within the byte = one physical `SDIk` line = one channel.**

- Byte `n` of a 24-byte slice carries bit `23 - n` of every channel's current
  sample -- MSB first, so byte 0 is sample bit 23. That is exactly
  "latch `DATA[15:0]`, shift, repeat 24 times".
- Bit 0 -> `SDI0`, bit 1 -> `SDI1`, bit 2 -> `SDI2` -- i.e. inputs 1, 2 and the
  mic ADC. Bits 3-7 would be `SDI3..SDI7` if populated; they are not, so they
  read zero.
- The odd slice (bytes 0-23) and even slice (bytes 32-55) are the **L and R
  sub-frames** of one I2S frame on those same lines. The 8-byte gaps
  (bytes 24-31, 56-63) are the non-significant bits of a 32-bit I2S slot:
  24 data bits + 8 pad, twice, = the 64-byte capture frame.

Playback (EP 0x05) is the mirror image but narrower and on the other bus: a
48-byte frame (24 + 24, no gap = 24-bit slots), bit 0 -> `SDO0`, bit 1 ->
`SDO1`, generated by the ATmega8515 and clocked out over the multiplexed
`AD[7:0]` bus via the 74HC4050.

Consequences:

- The frame geometry (24-byte slices, "8-channel" shape) is fixed by the I2S
  timing and the bus width, **not** by how many channels are actually wired.
  This is why the same Ploytec layout serves devices with very different
  channel counts, and why the driver's codec always works in 8-channel slices
  and masks off the unused bits.
- `DATA[15:0]` = `SDI15..SDI0` is 16 serial lines, each an I2S stereo pair, so
  the architecture tops out at **32 capture channels**. The Jockey 3 populates
  three (`SDI0..2` -> 6 channels); `SDI3..15` and `SDO2..7` are unpopulated.
- This is a structural model inferred from the schematic and the observed
  stream, not confirmed against a logic capture of the ISP1583 DMA FIFO. It
  fits every byte we have seen, but treat it as the working explanation, not a
  datasheet fact.

## LED Patterns

The following LED patterns have been observed and their meanings deduced:

| Pattern | When it happens | What it means |
|---------|-----------------|---------------|
| **Vertical sweep** (bottom-top-bottom, once) | Right after powering on | Hardware self-test / lamp test completed. |
| **Horizontal back-and-forth** (repeating) | Powered on, no USB/MIDI activity for ~1 min | Stand-alone demo / "attract mode". |
| **Normal MIDI response** | Connected to host + first MIDI OUT event received | Standard operation. The attract mode stops as soon as ANY MIDI event is sent to the device (e.g., toggling an LED via `amidi` or a DJ application). |

The "attract mode" does not stop upon a successful handshake alone; it requires at least one MIDI OUT message to transition the firmware into normal operation mode. This has been confirmed with the current driver implementation.

