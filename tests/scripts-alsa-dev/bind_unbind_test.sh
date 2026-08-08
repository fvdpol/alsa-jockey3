#!/bin/bash


# The "Unbind/Bind" Stress Test 
# 
# Since we replaced automatic devres management with manual devm actions
# (jockey3_kfree_action), the most likely regression would be a memory leak
# or a double-free during device initialization failure or disconnection.

# Success Criteria: dmesg should show no "slab-out-of-bounds," "double
# free," or "invalid opcode" errors.  If the kfree was registered
# incorrectly, the machine would likely crash or log a kernel oops during
# the unbind step.

DEVICE="1-1:1.0"
cd /sys/bus/usb/drivers/snd-reloop-jockey3
# Cycle it 10 times
for i in {1..10}; do
     echo "Iteration $i: Unbinding..."
     sudo sh -c "echo '$DEVICE' > unbind"
     sleep 1
     echo "Iteration $i: Binding..."
     sudo sh -c "echo '$DEVICE' > bind"
     sleep 1
done

