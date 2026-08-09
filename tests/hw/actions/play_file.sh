#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Play a real audio file to the device, for JT-AUDIO-003.
#
#   play_file.sh <file> [rate]
#
# Real content, listened to, is the only test that catches codec packing
# errors as they actually manifest -- as distortion rather than as a wrong
# byte. No automated check here would add anything the ear does not already
# do better.
#
# sox handles the decode and the rate conversion so the device sees exactly
# the format it accepts, rather than relying on plughw to paper over a
# mismatch. Testing through plughw would test the conversion plugin.
#
# Derived from play_flac.sh in ../../scripts-alsa-dev.

set -u

FILE=${1:-}
RATE=${2:-44100}
DEVICE=${JT_DEVICE:-hw:Jockey3}

[ -n "$FILE" ] || { echo "usage: play_file.sh <file> [rate]" >&2; exit 2; }
[ -f "$FILE" ] || { echo "no such file: $FILE" >&2; exit 2; }
command -v sox >/dev/null || { echo "sox not installed" >&2; exit 3; }

echo "playing $(basename "$FILE") at ${RATE} Hz -> $DEVICE"

# Upmix stereo to the device's four channels: Master L/R and Headphone L/R get
# the same signal, so both outputs can be checked from one pass.
sox "$FILE" -r "$RATE" -c 4 -b 24 -e signed-integer -t raw - remix 1 2 1 2 \
	| aplay -D "$DEVICE" -r "$RATE" -c 4 --format S24_3LE -t raw
