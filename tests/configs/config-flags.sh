#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Shared symbol lists for tests/configs/*.config. Sourced by derive-prod.sh
# and check-debug-config.sh so the two can never disagree about what
# "debug" means for a given architecture.

# Everything that makes a debug kernel a debug kernel: CONFIG_*-debug.config
# carries these as =y, CONFIG_*-prod.config has them off.
#
# The lock-debugging entries are not redundant with PROVE_LOCKING: LOCKDEP is
# `select`ed by several symbols, so disabling PROVE_LOCKING alone leaves it on
# via DEBUG_LOCK_ALLOC, DEBUG_RT_MUTEXES and DEBUG_WW_MUTEX_SLOWPATH -- which
# would leave lock tracking active in the very configuration that exists to
# produce undistorted timing numbers.
DEBUG_ONLY=(
	KASAN KASAN_GENERIC KASAN_INLINE KASAN_OUTLINE
	PROVE_LOCKING PROVE_RCU LOCKDEP DEBUG_LOCKDEP LOCK_STAT
	DEBUG_LOCK_ALLOC DEBUG_RT_MUTEXES DEBUG_WW_MUTEX_SLOWPATH
	DEBUG_ATOMIC_SLEEP DEBUG_SPINLOCK DEBUG_MUTEXES DEBUG_PREEMPT
	DEBUG_LIST DEBUG_OBJECTS DEBUG_KMEMLEAK
	# The codec KUnit suite runs at every module load. Useful on a debug
	# kernel, explicitly not for production -- see its Kconfig help.
	KUNIT SND_USB_JOCKEY3_CODEC_KUNIT_TEST
)

# The subset of DEBUG_ONLY a -debug.config is actually required to carry as
# =y. The rest of DEBUG_ONLY (KASAN_OUTLINE, an alternative to KASAN_INLINE
# rather than an addition to it; DEBUG_LOCKDEP and LOCK_STAT, deeper lockdep
# instrumentation nobody has needed here yet) must still be disabled in
# -prod if present, but a -debug config is not wrong for leaving them off.
# check-debug-config.sh checks -debug against this list, not DEBUG_ONLY.
DEBUG_REQUIRED=(
	KASAN KASAN_GENERIC KASAN_INLINE
	PROVE_LOCKING PROVE_RCU LOCKDEP
	DEBUG_LOCK_ALLOC DEBUG_RT_MUTEXES DEBUG_WW_MUTEX_SLOWPATH
	DEBUG_ATOMIC_SLEEP DEBUG_SPINLOCK DEBUG_MUTEXES DEBUG_PREEMPT
	DEBUG_LIST DEBUG_OBJECTS DEBUG_KMEMLEAK
	KUNIT SND_USB_JOCKEY3_CODEC_KUNIT_TEST
)

# DEBUG_REQUIRED symbols that a specific architecture's Kconfig can never
# provide -- not a misconfiguration, an actual absence of the feature there.
# "arch:SYMBOL" pairs, checked against DEBUG_REQUIRED by check-debug-config.sh
# and config_check.py before either treats an unset symbol as a failure.
#
# i386: mainline x86 KASAN has only ever supported X86_64
# (`select HAVE_ARCH_KASAN if X86_64` in arch/x86/Kconfig) -- 32-bit x86 has
# no KASAN, generic or otherwise, and never will short of upstream adding it.
DEBUG_REQUIRED_EXEMPT=(
	i386:KASAN i386:KASAN_GENERIC i386:KASAN_INLINE
)

# Live under DEBUG_KERNEL but stay on in both -debug and -prod: the test
# framework depends on these rather than merely benefiting from them. See
# tests/configs/README.md for what breaks without each one.
ALWAYS_ON=(
	DYNAMIC_DEBUG DEBUG_FS IKCONFIG_PROC DETECT_HUNG_TASK
	MAGIC_SYSRQ KALLSYMS_ALL WQ_WATCHDOG SND_PCM_XRUN_DEBUG
)

# Must never be on, in either configuration: locally built modules would not
# load at all.
ALWAYS_OFF=(
	MODULE_SIG_FORCE
)
