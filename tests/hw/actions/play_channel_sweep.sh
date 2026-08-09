#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Play a tone to each playback channel in turn, for JT-AUDIO-001.
#
#   play_channel_sweep.sh [rate] [seconds]
#
# Channel order and pitch match the playback channel map, so the operator can
# check placement and rate in one pass:
#
#   1  Master L      E3
#   2  Master R      A3
#   3  Headphone L   D4
#   4  Headphone R   G4
#
# The pitch matters as much as the placement: a tone at the wrong pitch means
# the rate was not actually applied, which a "did you hear it?" check alone
# would miss.
#
# Derived from the test_channels_*.sh family in ../../scripts-alsa-dev.

set -u

RATE=${1:-44100}
SECONDS_PER_CH=${2:-2}
DEVICE=${JT_DEVICE:-hw:Jockey3}

command -v sox  >/dev/null || { echo "sox not installed" >&2;  exit 3; }
command -v aplay >/dev/null || { echo "aplay not installed" >&2; exit 3; }

gen() {  # note  remix-spec
	sox -n -r "$RATE" -c 4 -b 24 -e signed-integer -t raw - \
		synth "$SECONDS_PER_CH" sine "$1" gain -6 remix $2
}

echo "sweep at ${RATE} Hz -> $DEVICE"
echo "  Master L (E3), Master R (A3), Headphone L (D4), Headphone R (G4)"

{
	gen E3 "1 0 0 0"
	gen A3 "0 1 0 0"
	gen D4 "0 0 1 0"
	gen G4 "0 0 0 1"
} | aplay -D "$DEVICE" -r "$RATE" -c 4 --format S24_3LE -t raw
