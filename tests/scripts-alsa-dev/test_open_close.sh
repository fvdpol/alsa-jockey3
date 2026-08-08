

aplay -l

while true; do
	sox -n -r 44100 -c 4 -b 24 -e signed-integer -t raw - synth 0.5 sine E3 gain -6 | aplay -D hw:1,0 -r 44100 -c 4 --format S24_3LE
done
