#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Switch which installed kernel a Raspberry Pi target actually boots.
#
#   switch_kernel.sh <target> [path-to-deb]
#
# THIS SCRIPT ASKS FOR A PASSWORD, AND THAT IS DELIBERATE
# ------------------------------------------------------
# Installing a kernel is root by another name, same reasoning as
# reload_driver.sh for modules: an occasional, deliberate act, not something
# a sudoers rule or jockey3-testctl verb should hand out.
#
# WHY THIS EXISTS
# ----------------
# /etc/kernel/postinst.d/z50-raspi-firmware does not copy the kernel package
# that was just installed -- it recomputes what it thinks is "the latest"
# kernel on every postinst run:
#
#   latest_kernel=$(find /boot -maxdepth 1 -name "vmlinuz-*-rpi-$flavour" \
#                    -print0 | sort -z -V -r | head -z -n1)
#
# It globs every vmlinuz-*-rpi-<flavour> file already in /boot, not just the
# one from the package being installed, and picks the "highest" by
# `sort -V`. Our debug and prod builds of the same commit share one numeric
# kernel version by design -- see targets.yaml's note on LOCALVERSION, which
# is what lets `uname -r` self-identify the target -- so the only difference
# `sort -V` has to compare is the trailing "-alsa-debug" vs "-alsa-prod" text,
# and it falls back to plain lexicographic comparison there: 'p' > 'd', so
# `-alsa-prod-rpi-v8` always wins over `-alsa-debug-rpi-v8`, regardless of
# which one was actually installed most recently or which one apt just ran.
# `apt install ./linux-image-*-alsa-debug*.deb` succeeds and prints nothing
# wrong; the box still boots prod. Found and confirmed on pi4test 2026-09-19
# (sha256sum of /boot/firmware/kernel8.img matched the prod vmlinuz right
# after a clean debug install).
#
# Patching z50-raspi-firmware itself was considered and rejected: it is a
# vendor script, gets silently overwritten by the next `raspi-firmware`
# package upgrade, and its sort-newest-wins behaviour is reasonable for its
# actual job of never auto-downgrading a normal Pi -- it is only wrong for
# us because two of our targets deliberately share a kernel version. So this
# works around it instead of fighting it: never leave two "-rpi-<flavour>"
# kernel packages installed at once. Remove every other one sharing this
# target's raspi-firmware flavour token before installing the one asked for,
# so z50's sort has nothing to prefer.
#
# Verifies the outcome by checksum rather than trusting the postinst output,
# since this whole script exists because that output lies by omission.

set -eu

TARGET=${1:-}
DEB=${2:-}
[ -n "$TARGET" ] || {
	echo "usage: switch_kernel.sh <target> [path-to-deb]" >&2
	echo "  e.g. switch_kernel.sh arm64-debug" >&2
	exit 2
}

LIBDIR=$(dirname "$0")/../lib
CONF=$LIBDIR/machineconf.py
REPO=$(dirname "$0")/../../..

read -r ARCH LOCALVERSION <<<"$(cd "$REPO" && python3 - "$TARGET" <<'PY'
import sys
sys.path.insert(0, "tests/hw")
from lib import yamlio
with open("tests/hw/targets.yaml", encoding="utf-8") as f:
    targets = yamlio.safe_load(f)["targets"]
spec = targets.get(sys.argv[1])
if not spec:
    sys.exit("unknown target '%s'; have: %s" % (sys.argv[1], ", ".join(targets)))
lv = spec.get("localversion")
if not lv:
    sys.exit("target '%s' has no localversion in targets.yaml" % sys.argv[1])
print(spec["arch"], lv)
PY
)"

case "$ARCH" in
arm64) HOST_ARCH=aarch64 ;;
armhf) HOST_ARCH=armv7l  ;;
*)
	echo "target '$TARGET' is $ARCH, which raspi-firmware does not package" \
	     "this way -- switch_kernel.sh is for the Pi targets only." >&2
	exit 2
	;;
esac
[ "$(uname -m)" = "$HOST_ARCH" ] || {
	echo "this machine is $(uname -m), target '$TARGET' wants $HOST_ARCH" >&2
	exit 2
}

# --------------------------------------------------------------- get the deb
if [ -z "$DEB" ]; then
	BUILD_HOST=$(python3 "$CONF" get paths.build_host JT_BUILD_HOST alsa-dev)
	echo "looking for a $TARGET kernel package on $BUILD_HOST..."
	remote_path=$(ssh "$BUILD_HOST" \
		"ls -t ~/kbuild/linux-image-*'$LOCALVERSION'-rpi-*_*.deb 2>/dev/null | head -1")
	[ -n "$remote_path" ] || {
		echo "no linux-image-*${LOCALVERSION}-rpi-*_*.deb under ~/kbuild on" \
		     "$BUILD_HOST -- build one with build_kernel.sh $TARGET --package," \
		     "or pass a local .deb path as the second argument." >&2
		exit 2
	}
	DEB=$(mktemp -d)/$(basename "$remote_path")
	echo "fetching $remote_path..."
	scp -q "$BUILD_HOST:$remote_path" "$DEB"
fi
[ -f "$DEB" ] || { echo "no such file: $DEB" >&2; exit 2; }

PKGNAME=$(dpkg-deb -f "$DEB" Package)
RELEASE=${PKGNAME#linux-image-}
[ "$RELEASE" != "$PKGNAME" ] || {
	echo "$DEB does not look like a linux-image package ($PKGNAME)" >&2
	exit 2
}
case "$RELEASE" in
*"$LOCALVERSION"*) ;;
*)
	echo "warning: $DEB's release '$RELEASE' does not contain this target's" \
	     "LOCALVERSION '$LOCALVERSION' -- proceeding anyway since a" \
	     "package path was given explicitly." >&2
	;;
esac
FLAVOUR=${RELEASE##*-rpi-}
[ "$FLAVOUR" != "$RELEASE" ] || {
	echo "$RELEASE has no '-rpi-<flavour>' suffix -- not a raspi-firmware" \
	     "kernel release, refusing to guess what z50 would glob for it." >&2
	exit 2
}

# ------------------------------------------------- clear the competing sort
echo "== other -rpi-$FLAVOUR kernels installed =="
mapfile -t OTHERS < <(dpkg-query -W -f '${Package}\n' 'linux-image-*' 2>/dev/null \
	| grep -E -- "-rpi-${FLAVOUR}\$" | grep -v -- "^${PKGNAME}\$" || true)
if [ "${#OTHERS[@]}" -eq 0 ]; then
	echo "  none"
else
	printf '  %s\n' "${OTHERS[@]}"
	echo "removing them so z50-raspi-firmware's 'sort -V' has nothing to" \
	     "prefer over $RELEASE..."
	sudo apt remove -y "${OTHERS[@]}"
fi

# ------------------------------------------------------------------ install
echo "installing $PKGNAME (this is the step that needs a password)..."
sudo apt install -y --reinstall "$DEB"

# -------------------------------------------------------------------- verify
have=$(sha256sum /boot/firmware/kernel8.img 2>/dev/null | awk '{print $1}')
want=$(sha256sum "/boot/vmlinuz-$RELEASE" 2>/dev/null | awk '{print $1}')
if [ -z "$want" ]; then
	echo "warning: /boot/vmlinuz-$RELEASE not found to verify against" >&2
elif [ "$have" != "$want" ]; then
	echo "kernel8.img does NOT match vmlinuz-$RELEASE after install -- z50" >&2
	echo "still picked something else. Check for another -rpi-$FLAVOUR" >&2
	echo "kernel this script missed (dpkg -l | grep linux-image)." >&2
	exit 1
else
	echo "verified: /boot/firmware/kernel8.img matches vmlinuz-$RELEASE"
fi

echo "currently booted: $(uname -r)"
echo "reboot now to actually run $RELEASE."
