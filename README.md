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
- Capture-endpoint restart reliability after sample-rate changes (still fails in some cases; direction-aware stall detection and recovery added, awaiting on-hardware validation)
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
sudo insmod snd-reloop-jockey3.ko
```

TODO add suggestions on usage (audio, midi)


# Testing

The audio codec — the bit-scattering conversion between ALSA's `S24_3LE`
format and the Ploytec wire format — has its own test suite, because it exists
in three architecture-specific versions of which only one is compiled into any
given build, and the fast ones are not verifiable by reading.

```bash
cd tests

./run_kunit.sh          # in-kernel KUnit suite, under UML (seconds)
./run_kunit.sh --all    # and under QEMU: i386, arm64, arm, riscv

./codecbench.py test    # user-space: all three variants + candidates
./codecbench.py bench   # benchmark them on this machine
```

The KUnit suite ships with the driver and runs in the kernel; the user-space
bench compares every variant against the others and doubles as a workbench for
developing new ones. Both are checked against an independent model of the wire
format, so they cannot agree on a wrong answer.

See **[docs/testing.md](docs/testing.md)** for the full guide, including how to
develop and promote a new codec implementation.


# Technical Background
The driver was developed by analyzing USB traffic between the controller and official drivers (Windows/macOS) using an OpenVizsla USB protocol analyzer. Additional insights were drawn from the [Ozzy project](https://github.com/mischa85/Ozzy).


# Contributing

Contributions are very welcome! This is a complex reverse-engineered driver.
Areas especially appreciated:

- Testing on Master Edition
- Stability / error handling improvements
- Code review & refactoring
- Documentation

## Coding style

This driver targets inclusion in the mainline Linux kernel, so
[the kernel coding style](https://www.kernel.org/doc/html/latest/process/coding-style.html)
applies throughout. Beyond that, a few project-specific conventions:

- **American English.** The kernel overwhelmingly uses American spelling
  (`initialize` outnumbers `initialise` roughly 14:1 in-tree, `optimize` over
  `optimise` 25:1), so use `initialize`, `serialize`, `synchronization`,
  `behavior`, `optimize`. This applies to identifiers as well as comments.
  Note that neither `scripts/spelling.txt` nor `codespell` flags British
  spellings, so this is not caught automatically.

- **Two namespaces.** `jockey3_*` for the ALSA/USB glue that is specific to
  this card; `ploytec_*` for the hardware protocol, codec and firmware-quirk
  layer, which is in principle shared with other Ploytec-based devices.

- **Comments.** `/* */` for substantial or multi-line explanations; `//` for
  short single-line notes, such as recording where a magic value came from.

- **Locking.** The lock hierarchy is documented at the top of `jockey3.c`.
  Anything reachable from a PCM `.trigger` or `.pointer` callback runs in
  atomic context and must not sleep.

Before submitting, run the checks the kernel itself uses:

```sh
scripts/checkpatch.pl --strict --codespell -g <range>
scripts/kernel-doc -Wall -Werror --none <file>.c
make W=1 C=1 M=sound/usb/jockey3      # sparse
```

# License

This project is licensed under the GPL-2.0-or-later, matching the Linux kernel
it is intended to be merged into. See [LICENSE](LICENSE) for details.


# Related Projects

[Ozzy](https://github.com/mischa85/Ozzy) — Another Ploytec-based device driver, supporting on Allen & Heath devices