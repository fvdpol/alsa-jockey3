#!/bin/bash

# Test script to loop through sample rates and check for mismatches in dmesg

RATES=(44100 48000 88200 96000)
CARD="RJ3"

echo "Starting Sample Rate Switching Stress Test..."
echo "Monitoring dmesg for 'ploytec:' and 'jockey3:' messages."

# Clear dmesg or mark the start
sudo dmesg -C

for i in {1..50}; do
    RATE=${RATES[$RANDOM % ${#RATES[@]}]}
    echo "[$i/50] Testing Sample Rate: $RATE Hz"
    
    # Use speaker-test to trigger hw_params and start a stream
    # -l 1 means 1 loop, -t sine for a tone
    # Redirecting stdout/stderr to /dev/null to keep terminal clean
    speaker-test -D hw:$CARD -r $RATE -c 4 -t sine -l 1 > /dev/null 2>&1
    
    # Check for warnings or errors in dmesg
    MISMATCH=$(sudo dmesg | grep -iE "Rate mismatch|Failed to set rate")
    if [ ! -z "$MISMATCH" ]; then
        echo "FAIL: Detected rate mismatch or failure!"
        echo "$MISMATCH"
        # We don't exit, we want to see how often it happens
    fi
    
    # Small sleep between switches
    sleep 0.5
done

echo "Test Complete."
sudo dmesg | grep -iE "ploytec:|jockey3:"
