# Linux ALSA Reloop Jockey 3 Driver

This is a work-in-progress to develop a Linux ALSA driver for the (discontinued) Reloop Jockey 3 DJ Controller.

Unlike most newer devices this devices does not offer a class compliant USB interface for audio and midi data, but uses a proprietary protocol. 
Vendor drivers are required to operate this device, which were only shipped for Windows and MacOS. Since the device is now discontinued it does not receive driver updates anymore.

## Current Status

- Functional MIDI Receive and Transmit
- Functional Audio playback 4 channels; 
- Functional Audio capture 6 channels
- Supports dynamic rate switching, supporting 44.1 kHz, 48 kHz, 88.2 kHz and 96 kHz

### Pending

- Handling rate switching corner cases
- Error/disconnet handling 
- Improvements in code quality
- Long-term stability testing
- Integration in Linux kernel tree

## Supported Hardware

| Device | Status | 
|--------|--------|
| Reloop Jockey 3 Master Edition |  should work, untested | 
| Reloop Jockey 3 Remix | Tested | 


theoretically this driver should the Reloop Jockey 3 Master Edition controller, but I do not have access to this hardware to validate. Please share feedback if you are able to test.


