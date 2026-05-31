# ALSA Driver for Reloop Jockey 3

[![License](https://img.shields.io/github/license/fvdpol/alsa-jockey3)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/fvdpol/alsa-jockey3)](https://github.com/fvdpol/alsa-jockey3/stargazers)

**Linux ALSA driver for the Reloop Jockey 3 DJ controller** 

Unlike most modern DJ controllers, the Reloop Jockey 3 does not use a class-compliant USB audio/MIDI interface. It relies on a proprietary USB protocol developed by **Ploytec GmbH**. This driver aims to provide native Linux support via ALSA.

## Features

- **MIDI**: Full bidirectional support (in/out) of the control surface
- **Audio**:
  - Playback: 4 channels
  - Capture: 6 channels
- **Sample Rates**: Dynamic switching between 44.1 kHz, 48 kHz, 88.2 kHz, and 96 kHz
- **Low-level USB protocol** reverse-engineered via OpenVizsla + Windows/macOS driver analysis

## Current Status

**Working**
- Audio playback and capture
- MIDI I/O
- Rate switching

**Pending / In Progress**
- Long-term stability testing
- Confirmation of other Reloop Jockey 3 hardware
- Kernel tree integration (eventual goal)

## Supported Devices

| Device                        | Status              | Notes                     |
|-------------------------------|---------------------|---------------------------|
| Reloop Jockey 3 Remix         | ✅ Tested & Working | Primary development target |
| Reloop Jockey 3 Master Edition| ⚠️ Untested         | Should work — feedback welcome |

> **Note**: I do not personally own a Master Edition. Testing reports from users with this model would be very helpful.

## Installation

### Prerequisites

- Linux kernel headers (`linux-headers-$(uname -r)`)
- `build-essential`, `git`
- ALSA utils (`alsa-utils`)

### Build & Install

```bash
git clone https://github.com/fvdpol/alsa-jockey3.git
cd alsa-jockey3
make
sudo make install
```

TODO add suggestions on usage (audio, midi)


# Technical Background
The driver was developed by analyzing USB traffic between the controller and official drivers (Windows/macOS) using an OpenVizsla USB protocol analyzer. Additional insights were drawn from the [Ozzy project](https://github.com/mischa85/Ozzy).


# Contributing

Contributions are very welcome! This is a complex reverse-engineered driver.
Areas especially appreciated:

- Testing on Master Edition
- Stability / error handling improvements
- Code review & refactoring
- Documentation

# License

This project is licensed under the GPLv3. See [LICENSE](LICENSE.md) for details.


# Related Projects

[Ozzy](https://github.com/mischa85/Ozzy) — Another Ploytec-based device driver, supporting on Allen & Heath devices