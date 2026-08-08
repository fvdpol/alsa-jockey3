#!/bin/bash
# test_alsa_limits.sh

CARD="hw:RJ3" # Change to your actual ALSA card identifier
DURATION=5        # Seconds to test each configuration
SAMPLE_RATE=44100 # Test across 44100 and 48000
CHANNELS=4
FORMAT="S24_3LE"   # Adjust to match your hardware format (e.g., S32_LE)

# Generate a continuous test tone using SoX
sox -n -r $SAMPLE_RATE -c $CHANNELS -b 24 test_tone.wav synth $DURATION sine 440

echo "Starting automated ALSA constraint boundaries test..."

# Iterate through period sizes (in frames)
for period in 4 8 16 32 48 64 128 256 512; do
    # Iterate through periods (number of buffers)
    for periods in 2 3 4 8; do
        buffer=$((period * periods))
        
        echo "--------------------------------------------------------"
        echo "Testing: Period Size=${period} frames, Periods=${periods} (Buffer Size=${buffer} frames)"
        
        # Run aplay and capture stderr to look for Xruns
        # -D forces specific hardware parameters if the driver allows the range
        OUTPUT=$(aplay -D $CARD --period-size=$period --buffer-size=$buffer -t wav test_tone.wav 2>&1)
        
        if echo "$OUTPUT" | grep -iq "underrun"; then
            echo "RESULT: FAIL (Xrun detected)"
        elif echo "$OUTPUT" | grep -iq "Invalid argument"; then
            echo "RESULT: REJECTED (Outside current driver hardware constraints)"
        else
            echo "RESULT: SUCCESS"
        fi
    done
done

#rm test_tone.wav
