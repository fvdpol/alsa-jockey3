#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Play silence to the Jockey 3, used to reset the device to 44.1 kHz.

aplay -D hw:RJ3 -r 44100 -c 4 --format S24_3LE -d 5 /dev/zero
