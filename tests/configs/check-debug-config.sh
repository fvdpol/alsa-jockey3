#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Validate that tests/configs/<arch>-debug.config, and its -prod sibling if
# present, carry the flags the test framework expects -- independent of how
# either file was produced. A config built by hand, or derived backward from
# its sibling instead of through derive-prod.sh, can silently drift from what
# a target name is supposed to mean; this is what catches that.
#
#   ./check-debug-config.sh arm64
#   ./check-debug-config.sh --all

set -eu

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=tests/configs/config-flags.sh
. "$HERE/config-flags.sh"

state() { # file symbol -> y | n | absent
	if grep -qE "^CONFIG_$2=" "$1"; then
		echo y
	elif grep -qE "^# CONFIG_$2 is not set" "$1"; then
		echo n
	else
		echo absent
	fi
}

localversion() { # file
	grep -E '^CONFIG_LOCALVERSION=' "$1" | cut -d'"' -f2
}

check_symbols() { # file label want_on...=DEBUG_ONLY|ALWAYS_ON  (checked =y)
	local file=$1 label=$2; shift 2
	local sym st fail=0
	for sym in "$@"; do
		st=$(state "$file" "$sym")
		[ "$st" = y ] || { echo "$label: CONFIG_$sym should be =y (is $st)"; fail=1; }
	done
	return $fail
}

check_off() { # file label symbols...  (checked not =y)
	local file=$1 label=$2; shift 2
	local sym st fail=0
	for sym in "$@"; do
		st=$(state "$file" "$sym")
		[ "$st" != y ] || { echo "$label: CONFIG_$sym must not be =y"; fail=1; }
	done
	return $fail
}

check_arch() { # arch
	local arch=$1 fail=0
	local debug=$HERE/$arch-debug.config
	local prod=$HERE/$arch-prod.config

	[ -f "$debug" ] || { echo "$arch: no $arch-debug.config" >&2; return 2; }

	local lv
	lv=$(localversion "$debug")
	[ "$lv" = "-alsa-debug" ] || {
		echo "$arch-debug: LOCALVERSION is \"$lv\", want \"-alsa-debug\""; fail=1; }

	# Symbols this architecture's Kconfig can never provide (e.g. i386 has
	# no KASAN) are not checked as required, even though they still stay in
	# DEBUG_ONLY and so are still required to be OFF on the -prod side.
	local required=() sym exempt skip
	for sym in "${DEBUG_REQUIRED[@]}"; do
		skip=0
		for exempt in "${DEBUG_REQUIRED_EXEMPT[@]}"; do
			[ "$exempt" = "$arch:$sym" ] && skip=1
		done
		[ "$skip" = 1 ] || required+=("$sym")
	done
	check_symbols "$debug" "$arch-debug" "${required[@]}" || fail=1
	check_symbols "$debug" "$arch-debug" "${ALWAYS_ON[@]}" || fail=1
	check_off "$debug" "$arch-debug" "${ALWAYS_OFF[@]}" || fail=1

	if [ -f "$prod" ]; then
		lv=$(localversion "$prod")
		[ "$lv" = "-alsa-prod" ] || {
			echo "$arch-prod: LOCALVERSION is \"$lv\", want \"-alsa-prod\""; fail=1; }

		check_off "$prod" "$arch-prod" "${DEBUG_ONLY[@]}" || fail=1
		check_symbols "$prod" "$arch-prod" "${ALWAYS_ON[@]}" || fail=1
		check_off "$prod" "$arch-prod" "${ALWAYS_OFF[@]}" || fail=1
	else
		echo "$arch: no $arch-prod.config yet (run ./derive-prod.sh $arch)"
	fi

	[ "$fail" = 0 ] && echo "$arch: OK"
	return $fail
}

if [ "${1:-}" = "--all" ]; then
	status=0
	for f in "$HERE"/*-debug.config; do
		check_arch "$(basename "$f" -debug.config)" || status=1
	done
	exit $status
fi

[ $# -eq 1 ] || { echo "usage: $0 <arch>|--all" >&2; exit 2; }
check_arch "$1"
