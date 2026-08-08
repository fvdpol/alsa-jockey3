#!/bin/bash

echo "unload...."
sudo rmmod -v snd-reloop-jockey3
sleep 1

echo "fetch from kernel dev tree..."
scp frank@alsa-dev:~/sound/sound/usb/jockey3/snd-reloop-jockey3.ko .

sudo mkdir -p /lib/modules/$(uname -r)/kernel/sound/usb/jockey3
sudo cp -pv snd-reloop-jockey3.ko  /lib/modules/$(uname -r)/kernel/sound/usb/jockey3/
sudo depmod

sleep 1
echo "load...."
sudo modprobe snd-rawmidi
sudo insmod -v snd-reloop-jockey3.ko 

sudo ./set_debug.sh