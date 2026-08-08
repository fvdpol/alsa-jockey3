
aplay -l
(
   sox -n -r 96000 -c 4 -b 24 -e signed-integer -t raw - synth 2 sine E3 gain -6 remix 1 0 0 0
   sox -n -r 96000 -c 4 -b 24 -e signed-integer -t raw - synth 2 sine A3 gain -6 remix 0 1 0 0
   sox -n -r 96000 -c 4 -b 24 -e signed-integer -t raw - synth 2 sine D4 gain -6 remix 0 0 1 0
   sox -n -r 96000 -c 4 -b 24 -e signed-integer -t raw - synth 2 sine G4 gain -6 remix 0 0 0 1
) | aplay -D hw:1,0 -r 96000 -c 4 --format S24_3LE

