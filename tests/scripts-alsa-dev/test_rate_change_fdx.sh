

aplay -l

while true; do
	./test-led-single.sh

	arecord -vv -D hw:RJ3 -d 3 -f S24_3LE -c 6 -r 44100 -t wav > test_cap.wav &	
	sleep .1
	sox -n -r 44100 -c 4 -b 24 -e signed-integer -t raw - synth 4 sine E3 gain -6 | aplay -D hw:1,0 -r 44100 -c 4 --format S24_3LE
	sleep 2

	arecord -vv -D hw:RJ3 -d 3 -f S24_3LE -c 6 -r 48000 -t wav > test_cap.wav &		
	sleep .1
	sox -n -r 48000 -c 4 -b 24 -e signed-integer -t raw - synth 4 sine E3 gain -6 | aplay -D hw:1,0 -r 48000 -c 4 --format S24_3LE
	sleep 2
	
	arecord -vv -D hw:RJ3 -d 3 -f S24_3LE -c 6 -r 88200 -t wav > test_cap.wav &	
	sleep .1
	sox -n -r 88200 -c 4 -b 24 -e signed-integer -t raw - synth 4 sine E3 gain -6 | aplay -D hw:1,0 -r 88200 -c 4 --format S24_3LE
	sleep 2
	
	arecord -vv -D hw:RJ3 -d 3 -f S24_3LE -c 6 -r 96000 -t wav > test_cap.wav &	
	sleep .1
	sox -n -r 96000 -c 4 -b 24 -e signed-integer -t raw - synth 4 sine E3 gain -6 | aplay -D hw:1,0 -r 96000 -c 4 --format S24_3LE
	sleep 2
done
