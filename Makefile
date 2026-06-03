# Makefile for out-of-tree ALSA driver for Reloop Jockey 3 devices

obj-m += snd-reloop-jockey3.o
snd-reloop-jockey3-objs := jockey3.o ploytec_codec.o

# Build against the kernel on the target machine
#KDIR ?= /lib/modules/$(shell uname -r)/build
KDIR ?= /usr/src/linux-source-6.12

all:
	make -C $(KDIR) M="$(PWD)" modules

clean:
	make -C $(KDIR) M="$(PWD)" clean

#install:
#	make -C $(KDIR) M="$(PWD)" modules_install INSTALL_MOD_PATH=/tmp/mod-install  # for testing




