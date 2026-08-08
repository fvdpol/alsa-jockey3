#!/bin/bash

echo "unload...."
rmmod -v snd-reloop-jockey3
sleep 1

echo "fetch...."
scp frank@alsa-dev:~/jockey3_linux/alsa-jockey3/snd-reloop-jockey3.ko .

mkdir -p /lib/modules/$(uname -r)/kernel/sound/usb/reloop
cp -pv snd-reloop-jockey3.ko  /lib/modules/$(uname -r)/kernel/sound/usb/reloop/
depmod

sleep 1
echo "load...."
modprobe snd-rawmidi
insmod -v snd-reloop-jockey3.ko # debug=1
