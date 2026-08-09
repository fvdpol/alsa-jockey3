#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Install a freshly built module and reload it.
#
#   reload_driver.sh [path-to-ko]
#
# With no argument, fetches from the build host over ssh. Set JT_BUILD_HOST
# and JT_BUILD_PATH to point somewhere else, or pass a local path.
#
# Verifying the reload actually took effect is the caller's job, via
# /sys/module/snd_reloop_jockey3/srcversion: rmmod can fail silently if the
# module is in use, leaving the previous build resident and every subsequent
# result attributed to the wrong revision.
#
# Derived from reload_driver.sh and reload_bleeding_driver.sh in
# ../../scripts-alsa-dev.

set -eu

MODULE=snd-reloop-jockey3
KO=${1:-}
BUILD_HOST=${JT_BUILD_HOST:-alsa-dev}
BUILD_PATH=${JT_BUILD_PATH:-~/sound/sound/usb/jockey3/$MODULE.ko}
DEST=/lib/modules/$(uname -r)/kernel/sound/usb/jockey3

[ "$(id -u)" = 0 ] || { echo "must run as root" >&2; exit 3; }

if [ -z "$KO" ]; then
	KO=$(mktemp -d)/$MODULE.ko
	echo "fetching from $BUILD_HOST..."
	scp -q "$BUILD_HOST:$BUILD_PATH" "$KO"
fi
[ -f "$KO" ] || { echo "no module at $KO" >&2; exit 2; }

if lsmod | grep -q "^${MODULE//-/_} "; then
	echo "unloading..."
	rmmod "$MODULE"
fi

mkdir -p "$DEST"
# Deliberately not `cp -p`: preserving the source timestamp has repeatedly
# caused tooling downstream to conclude nothing changed and skip work, which
# silently leaves the previous revision in play.
cp -v "$KO" "$DEST/"
depmod

echo "loading..."
modprobe snd-rawmidi
modprobe "$MODULE"

echo "srcversion: $(cat /sys/module/${MODULE//-/_}/srcversion 2>/dev/null || echo '?')"
