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
# THIS SCRIPT ASKS FOR A PASSWORD, AND THAT IS DELIBERATE
# ------------------------------------------------------
# Copying a .ko into /lib/modules and loading it is arbitrary kernel code
# execution -- root by another name. So installing a module is the one
# privileged operation the test framework does NOT automate: it is not a verb
# in jockey3-testctl, and no sudoers rule grants it. Deploying a new build is
# an occasional, deliberate act; a password there costs nothing.
#
# Loading and unloading the already-installed module IS automated, through the
# helper, which is what lets an unattended profile cycle the driver.
#
# Verifying the reload actually took effect is the caller's job, via the
# module's build-id: rmmod can fail silently if the module is in use, leaving
# the previous build resident and every subsequent result attributed to the
# wrong revision.
#
# Derived from reload_driver.sh and reload_bleeding_driver.sh in
# ../../scripts-alsa-dev.

set -eu

MODULE=snd-reloop-jockey3
HELPER=/usr/local/sbin/jockey3-testctl
KO=${1:-}
# Where the built module comes from is a property of this machine, so it is
# read from ~/.config/jockey3/machine.yaml with JT_BUILD_* still overriding for
# a one-off. The old default pointed at ~/sound, which is the in-tree build for
# the build host's OWN kernel -- deploying that to a target gives a vermagic
# mismatch, since the loadable per-target module is what build_module.sh puts
# in ~/kbuild/<target>. Hence no built-in default for the path any more: an
# unconfigured machine should say so rather than fetch the wrong file.
LIBDIR=$(dirname "$0")/../lib
CONF=$LIBDIR/machineconf.py
BUILD_HOST=$(python3 "$CONF" get paths.build_host JT_BUILD_HOST alsa-dev)
BUILD_PATH=$(python3 "$CONF" get paths.build_path JT_BUILD_PATH || true)
DEST=/lib/modules/$(uname -r)/kernel/sound/usb/jockey3

if [ -z "$KO" ]; then
	if [ -z "$BUILD_PATH" ]; then
		echo "no module path configured: set paths.build_path in" >&2
		echo "~/.config/jockey3/machine.yaml, export JT_BUILD_PATH," >&2
		echo "or pass the .ko as an argument." >&2
		exit 2
	fi
	# build_module.sh writes each target's module to its own directory under
	# ~/kbuild/<target> (see docs/environments.md), so a single fixed
	# build_path would fetch whichever target happened to be built last --
	# exactly wrong when testing more than one target concurrently. A
	# {target} placeholder is filled in from what THIS machine is currently
	# booted as, via env.py's own LOCALVERSION-based detection, so there is
	# only one place that logic lives.
	case "$BUILD_PATH" in
	*'{target}'*)
		# env.py's own stderr already explains what did not match, e.g. an
		# unrecognized LOCALVERSION.
		if ! target=$(python3 "$LIBDIR/env.py" detect-target); then
			echo "cannot resolve {target} in paths.build_path -- pass the" >&2
			echo ".ko as an argument instead." >&2
			exit 2
		fi
		BUILD_PATH=${BUILD_PATH//\{target\}/$target}
		;;
	esac
	KO=$(mktemp -d)/$MODULE.ko
	echo "fetching $BUILD_PATH from $BUILD_HOST..."
	scp -q "$BUILD_HOST:$BUILD_PATH" "$KO"
fi
[ -f "$KO" ] || { echo "no module at $KO" >&2; exit 2; }

echo "unloading..."
sudo -n "$HELPER" unload || sudo "$HELPER" unload

# Installing needs real root; everything above and below goes through the
# helper. sudo will prompt here unless a password was cached.
echo "installing (this is the step that needs a password)..."
sudo mkdir -p "$DEST"
# Remove every compressed variant first. Distribution kernels install the
# module as snd-reloop-jockey3.ko.xz, and dropping an uncompressed .ko beside
# it leaves two modules of the same name in one directory -- from which
# depmod and modprobe may pick either. That is precisely the "reload silently
# left the previous build resident" failure this script's header warns about,
# and it is invisible until a result is attributed to the wrong revision.
for ext in .ko.xz .ko.zst .ko.gz; do
	sudo rm -fv "$DEST/${MODULE}${ext}"
done
# Deliberately not `cp -p`: preserving the source timestamp has repeatedly
# caused tooling downstream to conclude nothing changed and skip work, which
# silently leaves the previous revision in play.
sudo cp -v "$KO" "$DEST/"
sudo depmod

echo "loading..."
sudo -n "$HELPER" load || sudo "$HELPER" load

bid=$(cat "/sys/module/${MODULE//-/_}/notes/.note.gnu.build-id" 2>/dev/null \
	| od -An -tx1 | tr -d ' \n' | tail -c 40)
echo "build-id: ${bid:-?}"
