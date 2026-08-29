# Jockey 3 Hardware

Analysis based on the Service Manual for the reloop Jockey 3 Master Edition.

## Processors

The board carries three programmable devices. Only the first is on the path the
Linux driver talks to.

### ATmega8515 + ISP1583 -- the USB / "Ploytec" audio path

- **IC10, ATmega8515** runs the Ploytec bulk-audio protocol. It has 8 KB of
  on-chip flash. The vendor firmware image shipped with the macOS updater
  (`jockey3_remix_106.bin`, ~6 KB, lightly obfuscated -- strings are XOR 0xFF,
  ASCII version header `10060001`) is a near-full single image for this part
  and contains firmware for this MCU only.
- **IC9, ISP1583BS** is the USB device controller the ATmega drives.
- **The ISP1583 runs in Split bus mode.** Its `BUS_CONF/DA0` pin has a
  pull-down (= LOW), which per the datasheet selects:
  - `AD[7:0]` -- 8-bit local microprocessor bus, multiplexed address and data,
    shared with the ATmega8515.
  - `DATA[15:0]` -- a separate 16-bit DMA data bus.
  - control: `CS_N`, `ALE`/`A0` (per `MODE1`), `RW_N`+`DS_N` or `RD_N`+`WR_N`
    (per `MODE0/DA1`); DMA strobes `DIOR` / `DIOW`.
- **Capture and playback use different ISP1583 buses:**
  - *Capture (6 channels).* Three input ADCs' serial outputs go straight to the
    low three lines of the ISP1583 DMA bus and are clocked in over the
    dedicated `DATA[15:0]` interface:

    | ADC | Source | DMA line | Codec bit | ALSA capture channels |
    |---|---|---|---|---|
    | IC307 PCM1804  | Input 1     | `SDI0` -> `DATA0` | bit 0 | 1 (L) / 2 (R) |
    | IC320 PCM1804  | Input 2     | `SDI1` -> `DATA1` | bit 1 | 3 (L) / 4 (R) |
    | IC309 PCM1803A | Microphone  | `SDI2` -> `DATA2` | bit 2 | 5 / 6 (mono, L=R) |

  - *Playback (4 channels).* The output DACs are driven from `SDO0`, `SDO1`
    (via IC7, a 74HC4050), which ride the multiplexed `AD[7:0]` bus shared
    between the ISP1583 and the ATmega8515. `SDO0` carries the odd channels
    (ALSA 1/3, Master L + Headphone L), `SDO1` the even channels (ALSA 2/4).
  - The schematic defines `SDI3..15` and `SDO2..7` but they are unpopulated.
- This split is the concrete meaning of "the capture DMA path": the
  `bRequest 0x49` / `wIndex 0` byte the driver treats as a "status register"
  (`re/ploytec_status_register.md`) is handled by the ATmega firmware and
  reconfigures the ISP1583 bulk-IN DMA over that `DATA[15:0]` interface, which
  is why rewriting it clears a stuck capture stream.
- The ADC serial bitstreams appear to be clocked straight into the ISP1583 DMA
  FIFO, which explains the Ploytec "bit-interleaved" codec format directly --
  each captured byte is one bit-clock snapshot of the `DATA[15:0]` lines. See
  `re/protocol_analysis.md`, "Why the format looks like this". Because
  `DATA[15:0]` = `SDI15..SDI0`, the architecture could carry up to 32 capture
  channels; the Jockey 3 wires three (`SDI0..2`).

### STM8S207 -- the control surface MCU

- Scans the faders, knobs, buttons and jog wheels, and drives the LEDs.
- Its data reaches the host as raw MIDI, multiplexed through the Ploytec bulk
  stream. It has an SPI link to the DSP56374 for the stand-alone mixer.
- Not addressed directly by the driver; "PIC" in vendor-binary symbol names
  (e.g. `XeUSBPrePicCommand`) is generic library terminology, not this part.

### DSP56374 -- stand-alone mixer only, not driver-relevant

- **IC710**, used for the Jockey 3's stand-alone (no-computer) mixer. Connects
  to the "D"-tagged ADC/DAC set and, via SPI, to the STM8S207.
- Fully self-contained: 20K x 24 program + bootstrap ROM, 6K x 24 program RAM,
  6K x 24 each X/Y data RAM plus 4K x 24 X/Y data ROM. It self-boots from
  on-chip ROM with no external boot memory, so there is no DSP program load in
  the ATmega firmware blob or the host driver.
- Whether the DSP code can be *patched* in the field is not settled. The
  schematic defines a JTAG connector (unknown whether it is populated on
  production units), and the DSP56374 datasheet describes a "PROM patching
  mechanism", so pushing a patch over the STM8S207 SPI link cannot be ruled
  out -- it would be a sensible thing for the hardware designer to have
  enabled. Either way it does not involve the Ploytec MCU or the driver.
- **Not wired to the ATmega8515 / ISP1583 at all.** Irrelevant to the driver;
  documented here only for the hardware picture. The codec/register-write
  bursts seen in the vendor driver are converter/PLL/clock setup on the Ploytec
  path, not DSP loads.

### No CPLD

There is no CPLD on the board. The vendor driver's `setEsuCpldByte` path
(`bRequest 0x49`, `wIndex 1`) is for a different Ploytec/ESI product and is
gated off by USB VID/PID for the Jockey 3.

## Converters and routing

IC303 - LC78212 analog switch for routing


IC305 - PCM1690 (vout 1,2 -> master out; vout 5,6 -> headphones)  "P" output?
IC306 - PCM1690 (vout 1,2 -> master out; vout 5,6 -> headphones)  "D" output?

IC307 - PCM1804  P1 in L+R  -> Ploytec SDI0 (Input 1)
IC308 - PCM1804  D1 in L+R  -> DSP
IC309 - PCM1803A - mono signal to L+R in for microphone ADC -> Ploytec SDI2

IC320 - PCM1804  P2 in L+R  -> Ploytec SDI1 (Input 2)
IC321 - PCM1804  D2 in L+R  -> DSP


IC710 DSP56374 is used for the stand-alone mixing; connects to the "D" tagged converters  --> "DSP"



IC9 - ISP1583BS - USB controller
IC10 - MEGA8515 - the "ploytec" magic? 


The USB/"Ploytec" part of the schematic is interesting;
IC7 (74HC4050) sends 2 outputs from the MEGA8515 AD0, AD1 to the two output DACs, SDO0 and SDO1; NOTE:  the schematic has 6 more outputs (SDO2..7) which are not populated

the 3 input ADCs SDI0,SDI1,SDI2 are directly connected to the ISP1583 ; NOTE: the schematic as in total 16 inputs defined (SDI0...SDI15)

These SDIxx/SDOxx signals go the ADC/DAC with the "P" tag --> "Ploytec"?

So it looks like the device has actually two parallel paths, with duplicated ADC/DAC;  one set for the "DSP", and other set for "Ploytec"

