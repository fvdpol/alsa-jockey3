sox -n -r 44100 -c 4 -b 24 -e signed-integer -t raw - synth 120 sine E3 gain -6 | aplay -D hw:RJ3 -r 44100 -c 4 --format S24_3LE
