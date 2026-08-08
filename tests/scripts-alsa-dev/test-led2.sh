# double-flash the 'Load' LEDs on both decks
#
amidi -p hw:1,0 -S "91 1b 7F 90 1b 7F"
sleep .1
amidi -p hw:1,0 -S "91 1b 00 90 1b 00"
sleep .1
amidi -p hw:1,0 -S "91 1b 7F 90 1b 7F"
sleep .1
amidi -p hw:1,0 -S "91 1b 00 90 1b 00"
sleep .1
amidi -p hw:1,0 -S "91 1b 7F 90 1b 7F"
sleep .1
amidi -p hw:1,0 -S "91 1b 00 90 1b 00"
