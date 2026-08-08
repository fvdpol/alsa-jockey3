

aplay -l

while true; do
	./test-led-single.sh
	sox -n -r 44100 -c 4 -b 24 -e signed-integer -t raw - synth 1 sine E3 gain -6 | aplay -D hw:1,0 -r 44100 -c 4 --format S24_3LE
	sox -n -r 48000 -c 4 -b 24 -e signed-integer -t raw - synth 1 sine E3 gain -6 | aplay -D hw:1,0 -r 48000 -c 4 --format S24_3LE
	sox -n -r 88200 -c 4 -b 24 -e signed-integer -t raw - synth 1 sine E3 gain -6 | aplay -D hw:1,0 -r 88200 -c 4 --format S24_3LE
	sox -n -r 96000 -c 4 -b 24 -e signed-integer -t raw - synth 1 sine E3 gain -6 | aplay -D hw:1,0 -r 96000 -c 4 --format S24_3LE
done
