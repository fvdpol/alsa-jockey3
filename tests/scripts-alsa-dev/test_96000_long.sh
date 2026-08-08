sox -n -r 96000 -c 4 -b 24 -e signed-integer -t raw - synth 120 sine E3 gain -6 | aplay -D hw:RJ3 -r 96000 -c 4 --format S24_3LE
