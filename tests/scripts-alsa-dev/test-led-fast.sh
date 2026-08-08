# double-flash the 'Load' LEDs on both decks
#
amidi -p hw:1,0 -S "90 1b 7F 91 1b 7F 90 23 7F 90 24 7F 90 33 7F 90 36 7F 91 23 7F 91 24 7F 91 33 7F 91 36 7F"
sleep .1
amidi -p hw:1,0 -S "90 1b 00 91 1b 00"
sleep .1

while true; do
	amidi -p hw:1,0 -S "90 21 00 90 09 0A"
	amidi -p hw:1,0 -S "90 21 01 90 09 09"
	amidi -p hw:1,0 -S "90 21 02 90 09 08"
	amidi -p hw:1,0 -S "90 21 03 90 09 07"
	amidi -p hw:1,0 -S "90 21 04 90 09 06"
	amidi -p hw:1,0 -S "90 21 05 90 09 05"
	amidi -p hw:1,0 -S "90 21 06 90 09 04"
	amidi -p hw:1,0 -S "90 21 07 90 09 03"
	amidi -p hw:1,0 -S "90 21 08 90 09 02"
	amidi -p hw:1,0 -S "90 21 09 90 09 01"
	amidi -p hw:1,0 -S "90 21 0A 90 09 00"

	amidi -p hw:1,0 -S "90 21 09 90 09 01"
	amidi -p hw:1,0 -S "90 21 08 90 09 02"
	amidi -p hw:1,0 -S "90 21 07 90 09 03"
	amidi -p hw:1,0 -S "90 21 06 90 09 04"
	amidi -p hw:1,0 -S "90 21 05 90 09 05"
	amidi -p hw:1,0 -S "90 21 04 90 09 06"
	amidi -p hw:1,0 -S "90 21 03 90 09 07"
	amidi -p hw:1,0 -S "90 21 02 90 09 08"
	amidi -p hw:1,0 -S "90 21 01 90 09 09"
done;

#amidi -p hw:1,0 -S "b0 1b 00 b1 1b 00"

#amidi -p hw:1,0 -S "90 1b 7F 91 1b 7F"
#sleep .1
#amidi -p hw:1,0 -S "90 1b 00 91 1b 00"
#sleep .1
#amidi -p hw:1,0 -S "90 1b 7F 91 1b 7F"
#sleep .1
#amidi -p hw:1,0 -S "90 1b 00 91 1b 00"
