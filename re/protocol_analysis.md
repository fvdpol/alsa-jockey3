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
