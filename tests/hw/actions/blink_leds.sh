#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Blink the 'Load' LEDs on both decks, for JT-MIDI-002.
#
#   blink_leds.sh [repeats]
#
# Doubles as proof that MIDI OUT reached the device at all: the device runs an
# LED attract animation until it receives its first MIDI OUT message, so the
# animation stopping is itself a positive result.
#
# Derived from test-led.sh in ../../scripts-alsa-dev.

set -u

REPEATS=${1:-3}
PORT=${JT_MIDI_PORT:-}

if [ -z "$PORT" ]; then
	# Resolve the rawmidi port by card name rather than assuming hw:1,0 --
	# the index depends on what else is plugged in, and hardcoding it is how
	# a test ends up talking to the wrong device and passing.
	PORT=$(amidi -l 2>/dev/null | awk '/Jockey/ {print $2; exit}')
fi
[ -n "$PORT" ] || { echo "no Jockey 3 rawmidi port found" >&2; exit 3; }

echo "blinking Load LEDs on $PORT"
for _ in $(seq 1 "$REPEATS"); do
	amidi -p "$PORT" -S "90 1b 7F 91 1b 7F"
	sleep .2
	amidi -p "$PORT" -S "90 1b 00 91 1b 00"
	sleep .2
done
