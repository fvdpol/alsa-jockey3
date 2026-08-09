#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Regenerate <arch>-prod.config from <arch>-debug.config.
#
#   ./derive-prod.sh x86_64
#
# The production configuration is the debug one with the debugging turned off,
# rather than an independently maintained file. That is deliberate: two
# hand-maintained configurations drift, and when they drift the difference
# between a prod and a debug result stops being "the debug options" and starts
# being "who knows". Deriving one from the other means the only difference is
# the list below.
#
# Environment:
#   KERNEL_SRC   kernel tree providing scripts/config and olddefconfig
#                (default ~/sound)

set -eu

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ARCH=${1:-x86_64}
KERNEL_SRC=${KERNEL_SRC:-$HOME/sound}

SRC=$HERE/$ARCH-debug.config
DST=$HERE/$ARCH-prod.config

[ -f "$SRC" ] || { echo "no such config: $SRC" >&2; exit 2; }
[ -x "$KERNEL_SRC/scripts/config" ] || {
	echo "no kernel tree at $KERNEL_SRC (set KERNEL_SRC)" >&2; exit 2; }

# Everything that makes the debug kernel a debug kernel.
#
# The lock-debugging entries are not redundant with PROVE_LOCKING: LOCKDEP is
# `select`ed by several symbols, so disabling PROVE_LOCKING alone leaves it on
# via DEBUG_LOCK_ALLOC, DEBUG_RT_MUTEXES and DEBUG_WW_MUTEX_SLOWPATH -- which
# would leave lock tracking active in the very configuration that exists to
# produce undistorted timing numbers.
DISABLE=(
	KASAN KASAN_GENERIC KASAN_INLINE KASAN_OUTLINE
	PROVE_LOCKING PROVE_RCU LOCKDEP DEBUG_LOCKDEP LOCK_STAT
	DEBUG_LOCK_ALLOC DEBUG_RT_MUTEXES DEBUG_WW_MUTEX_SLOWPATH
	DEBUG_ATOMIC_SLEEP DEBUG_SPINLOCK DEBUG_MUTEXES DEBUG_PREEMPT
	DEBUG_LIST DEBUG_OBJECTS DEBUG_KMEMLEAK
	# The codec KUnit suite runs at every module load. Useful on a debug
	# kernel, explicitly not for production -- see its Kconfig help.
	KUNIT SND_USB_JOCKEY3_CODEC_KUNIT_TEST
)

# Deliberately KEPT in production, despite living under DEBUG_KERNEL:
#   DYNAMIC_DEBUG      the firmware revision is only ever a dev_dbg
#   DEBUG_FS           dynamic debug is controlled through debugfs
#   IKCONFIG_PROC      /proc/config.gz, so a run can check its own target
#   MAGIC_SYSRQ        getting a task dump out of a wedged machine
#   DETECT_HUNG_TASK   the classifier treats a blocked task as a defect
#   WQ_WATCHDOG        the reset path runs on a workqueue; near-zero cost
#   SND_PCM_XRUN_DEBUG xrun reporting is a metric, not a debug aid

cp "$SRC" "$DST"
"$KERNEL_SRC/scripts/config" --file "$DST" --set-str LOCALVERSION "-alsa-prod"
for sym in "${DISABLE[@]}"; do
	"$KERNEL_SRC/scripts/config" --file "$DST" --disable "$sym"
done

# olddefconfig resolves the cascade: symbols that only existed because
# something disabled above selected them.
make -s -C "$KERNEL_SRC" olddefconfig KCONFIG_CONFIG="$DST"

echo "wrote $DST"
echo
printf '  %-34s %s\n' "LOCALVERSION" \
	"$(grep -E '^CONFIG_LOCALVERSION=' "$DST")"
for sym in KASAN PROVE_LOCKING LOCKDEP DEBUG_KMEMLEAK KUNIT; do
	printf '  %-34s %s\n' "$sym" \
		"$(grep -E "^CONFIG_$sym=" "$DST" || echo 'disabled')"
done
