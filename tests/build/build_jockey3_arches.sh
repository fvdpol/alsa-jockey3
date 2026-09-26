#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# JT-BUILD-005: cross-compile the module for every target architecture with
# W=1 and sparse, treating any new warning as a failure.
#
# Why this exists
# ----------------
# build_jockey3.sh's build gate (JT-BUILD-002) only ever builds for the host
# architecture, so an arch-specific warning -- such as -Wframe-larger-than
# tripping on 32-bit's tighter CONFIG_FRAME_WARN -- has no gate to catch it
# before a 0-day report does. See alsa-jockey3#53.
#
# This reuses the -debug half of each target's config pair (tests/configs/),
# because that is the only variant that builds
# CONFIG_SND_USB_JOCKEY3_CODEC_KUNIT_TEST=y -- the prod configs strip it, and
# the KUnit test file is exactly where the frame-size bug that prompted this
# script lived. ploytec_midi_kunit.c is deliberately not part of the kernel
# build (sync-driver.sh does not copy it) and is out of scope here.
#
# Usage:
#   ./build_jockey3_arches.sh                    all four architectures
#   ./build_jockey3_arches.sh i386 armhf          just these
#   JSON_REPORT=<path> ./build_jockey3_arches.sh  machine-readable report
#
# Environment:
#   BUILD_TREE    shared out-of-tree source, never built in-tree here
#                 (default ~/sound-build; see docs/environments.md)
#   BUILD_OUTPUT  root of per-arch object dirs   (default ~/kbuild-warngate)
#
# Each arch gets its own object dir under BUILD_OUTPUT, built up to
# 'modules_prepare' -- enough to compile an out-of-tree module against, far
# cheaper than build_kernel.sh's full vmlinux + .deb, and independent of
# ~/kbuild/<target> so this never collides with an object tree a hardware test
# run depends on.

set -u

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
BUILD_TREE=${BUILD_TREE:-$HOME/sound-build}
BUILD_OUTPUT=${BUILD_OUTPUT:-$HOME/kbuild-warngate}
DST=sound/usb/jockey3

[ -d "$BUILD_TREE" ] || { echo "no build tree at $BUILD_TREE" >&2; exit 2; }

ARCHES=("$@")
[ ${#ARCHES[@]} -eq 0 ] && ARCHES=(x86_64 i386 arm64 armhf)

case_arch() {  # arch -> KARCH CROSS CONFIG, space-separated
	case "$1" in
	x86_64) echo "x86  '' tests/configs/x86_64-debug.config" ;;
	i386)   echo "x86  '' tests/configs/i386-debug.config" ;;
	arm64)  echo "arm64 aarch64-linux-gnu- tests/configs/arm64-debug.config" ;;
	armhf)  echo "arm   arm-linux-gnueabihf- tests/configs/armhf-debug.config" ;;
	*) return 1 ;;
	esac
}

"$SRC_DIR/tests/build/sync-driver.sh" "$BUILD_TREE" >/dev/null

fails=0
declare -A METRICS
declare -a GATE_NAMES GATE_OK GATE_DETAIL

record() { GATE_NAMES+=("$1"); GATE_OK+=("$2"); GATE_DETAIL+=("$3"); }

for arch in "${ARCHES[@]}"; do
	spec=$(case_arch "$arch") || { echo "unknown arch '$arch'" >&2; exit 2; }
	# shellcheck disable=SC2086
	read -r KARCH CROSS CONFIG <<<"$spec"
	CROSS=${CROSS//\'/}
	CONFIG="$SRC_DIR/$CONFIG"

	if [ -n "$CROSS" ] && ! command -v "${CROSS}gcc" >/dev/null 2>&1; then
		echo "== $arch: skipped, missing cross compiler ${CROSS}gcc =="
		METRICS[built_$arch]=0
		record "arch_$arch" false "missing cross compiler ${CROSS}gcc"
		continue
	fi
	if [ ! -f "$CONFIG" ]; then
		echo "== $arch: skipped, no config at $CONFIG =="
		METRICS[built_$arch]=0
		record "arch_$arch" false "no config at $CONFIG"
		continue
	fi

	echo "== $arch (ARCH=$KARCH${CROSS:+ CROSS_COMPILE=$CROSS}) =="
	OBJ=$BUILD_OUTPUT/$arch-debug
	mkdir -p "$OBJ"
	cp "$CONFIG" "$OBJ/.config"

	makeargs=(-C "$BUILD_TREE" O="$OBJ" ARCH="$KARCH")
	[ -n "$CROSS" ] && makeargs+=(CROSS_COMPILE="$CROSS")

	if ! make "${makeargs[@]}" olddefconfig >/dev/null; then
		fails=$((fails + 1))
		record "arch_$arch" false "olddefconfig failed"
		continue
	fi
	if ! make -j"$(nproc)" "${makeargs[@]}" modules_prepare >/dev/null; then
		fails=$((fails + 1))
		record "arch_$arch" false "modules_prepare failed"
		continue
	fi

	# modules_prepare alone never produces a full Module.symvers -- that
	# needs a whole-kernel build, which is exactly the cost this script
	# exists to avoid -- so modpost sees every core-kernel symbol as
	# unresolved. That is expected and not what this gate checks;
	# KBUILD_MODPOST_WARN=1 keeps it from hard-failing over symbols that
	# a real build (build_module.sh, against a full object tree) already
	# verifies.
	buildlog=$(mktemp)
	KBUILD_MODPOST_WARN=1 make -j"$(nproc)" "${makeargs[@]}" M=$DST W=1 C=1 modules \
		2>&1 | tee "$buildlog"
	rc=${PIPESTATUS[0]}

	# Same benign -Wshadow hits as JT-BUILD-002 (build_jockey3.sh), filtered
	# for the same reason: they are correct observations about idiomatic
	# code, not defects.
	warns=$(grep -c -E '\bwarning:' "$buildlog" 2>/dev/null)
	benign=$(grep -E '\bwarning:' "$buildlog" 2>/dev/null |
		grep -c -E "declaration of .index. shadows|declaration of .__ret. shadows")
	warns=${warns:-0}
	benign=${benign:-0}
	METRICS[warnings_$arch]=$(( warns > benign ? warns - benign : 0 ))
	rm -f "$buildlog"

	if [ "$rc" -ne 0 ]; then
		METRICS[built_$arch]=0
		fails=$((fails + 1))
		record "arch_$arch" false "make exited $rc"
	elif [ "${METRICS[warnings_$arch]}" -ne 0 ]; then
		METRICS[built_$arch]=1
		fails=$((fails + 1))
		record "arch_$arch" false "${METRICS[warnings_$arch]} warning(s) at W=1 C=1"
	else
		METRICS[built_$arch]=1
		record "arch_$arch" true ""
	fi
done

METRICS[arches_built]=0
for arch in "${ARCHES[@]}"; do
	[ "${METRICS[built_$arch]:-0}" = 1 ] && METRICS[arches_built]=$((METRICS[arches_built] + 1))
done

if [ -n "${JSON_REPORT:-}" ]; then
	{
		printf '{\n  "gates": [\n'
		for i in "${!GATE_NAMES[@]}"; do
			[ "$i" -gt 0 ] && printf ',\n'
			printf '    {"name": "%s", "passed": %s, "detail": "%s"}' \
				"${GATE_NAMES[$i]}" "${GATE_OK[$i]}" "${GATE_DETAIL[$i]}"
		done
		printf '\n  ],\n  "metrics": {\n'
		first=1
		for k in "${!METRICS[@]}"; do
			[ "$first" -eq 0 ] && printf ',\n'
			printf '    "%s": %s' "$k" "${METRICS[$k]}"
			first=0
		done
		printf '\n  },\n  "fails": %s\n}\n' "$fails"
	} > "$JSON_REPORT"
fi

echo
if [ "$fails" -eq 0 ]; then
	echo "All architectures clean."
else
	echo "$fails architecture check(s) FAILED."
fi
exit "$fails"
