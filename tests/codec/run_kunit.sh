#!/bin/bash
# Run the Ploytec codec KUnit suite, optionally across architectures.
#
# Objective:
#   Prove the codec correct on machines that are not available physically.
#   The bit-plane codec hardcodes S24_3LE byte order and reaches for 32- and
#   64-bit words through the unaligned accessors, so word size, alignment
#   strictness and byte order all matter. UML covers the common case in
#   seconds; the cross targets cover the rest under QEMU.
#
# Why a separate tree:
#   kunit.py always builds out of tree, and the kernel refuses an out-of-tree
#   build when the source tree already holds an in-tree configuration. The
#   normal development tree (~/sound) is configured in-tree and is used by
#   build_jockey3.sh, so this script keeps its own git worktree instead of
#   asking anyone to run 'make mrproper' on a working tree.
#
# Usage:
#   ./run_kunit.sh                 run under UML (fastest)
#   ./run_kunit.sh arm64 riscv     run specific architectures
#   ./run_kunit.sh --all           um, i386, arm64, arm, riscv
#   ./run_kunit.sh --all --extra   also s390, as a big-endian proof
#   ./run_kunit.sh --ref um        force the portable reference codec
#   ./run_kunit.sh --list          show targets and toolchain status
#   ./run_kunit.sh --setup         create/refresh the worktree and stop
#   ./run_kunit.sh --clean         remove the build directories
#
# Environment:
#   KERNEL_SRC   main kernel tree to take the worktree from (default ~/sound)
#   KUNIT_TREE   worktree location                (default ~/sound-kunit)
#   KUNIT_REF    commit/branch to check out       (default feature/jockey3)
#
# (C) 2026 Frank van de Pol <fvdpol@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

set -u

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
KERNEL_SRC=${KERNEL_SRC:-$HOME/sound}
KUNIT_TREE=${KUNIT_TREE:-$HOME/sound-kunit}
KUNIT_REF=${KUNIT_REF:-feature/jockey3}
DST_REL=sound/usb/jockey3

DEFAULT_ARCHES="um i386 arm64 arm riscv"
EXTRA_ARCHES="s390"

use_reference_codec=0
fails=0

# target -> cross-compile prefix (empty means the host compiler is fine)
cross_prefix() {
	case "$1" in
	um|x86_64|i386) echo "" ;;
	arm64)          echo "aarch64-linux-gnu-" ;;
	arm)            echo "arm-linux-gnueabihf-" ;;
	riscv)          echo "riscv64-linux-gnu-" ;;
	s390)           echo "s390x-linux-gnu-" ;;
	*)              echo "" ;;
	esac
}

# target -> qemu system binary it needs (empty for UML)
qemu_binary() {
	case "$1" in
	um)      echo "" ;;
	x86_64)  echo "qemu-system-x86_64" ;;
	i386)    echo "qemu-system-i386" ;;
	arm64)   echo "qemu-system-aarch64" ;;
	arm)     echo "qemu-system-arm" ;;
	riscv)   echo "qemu-system-riscv64" ;;
	s390)    echo "qemu-system-s390x" ;;
	*)       echo "" ;;
	esac
}

# qemu binary -> Debian package that ships it
qemu_package() {
	case "$1" in
	qemu-system-i386|qemu-system-x86_64)     echo "qemu-system-x86" ;;
	qemu-system-arm|qemu-system-aarch64)     echo "qemu-system-arm" ;;
	qemu-system-riscv64|qemu-system-s390x)   echo "qemu-system-misc" ;;
	*)                                       echo "" ;;
	esac
}

# Which packages does this target still need? Empty output means it is ready.
missing_for() {
	local arch=$1 prefix qemu missing=""
	prefix=$(cross_prefix "$arch")
	qemu=$(qemu_binary "$arch")

	if [ -n "$prefix" ] && ! command -v "${prefix}gcc" >/dev/null 2>&1; then
		# gcc-aarch64-linux-gnu from aarch64-linux-gnu-
		missing="gcc-${prefix%-}"
	fi

	if [ -n "$qemu" ] && ! command -v "$qemu" >/dev/null 2>&1; then
		missing="$missing $(qemu_package "$qemu")"
	fi

	# riscv additionally needs the OpenSBI firmware blob, and kunit.py
	# exits rather than reporting a missing-tool error if it is absent.
	if [ "$arch" = riscv ] &&
	   [ ! -f /usr/share/qemu/opensbi-riscv64-generic-fw_dynamic.bin ]; then
		missing="$missing opensbi"
	fi

	echo "${missing# }"
}

# Is everything this target needs actually installed?
target_ready() {
	[ -z "$(missing_for "$1")" ]
}

setup_tree() {
	if [ ! -d "$KUNIT_TREE" ]; then
		echo "== creating worktree $KUNIT_TREE from $KERNEL_SRC ($KUNIT_REF) =="
		git -C "$KERNEL_SRC" worktree add --detach "$KUNIT_TREE" "$KUNIT_REF" \
			|| { echo "  *** FAILED to create the worktree"; exit 1; }
	fi

	local dst="$KUNIT_TREE/$DST_REL"
	mkdir -p "$dst"

	# Deliberately plain cp, not 'cp -p'. Preserving timestamps makes the
	# copy older than the object files from the previous run, and make then
	# skips the rebuild - which silently tests the previous revision.
	cp "$SRC_DIR/Kconfig"         "$dst/Kconfig"
	cp "$SRC_DIR/Makefile.kernel" "$dst/Makefile"
	cp "$SRC_DIR/.kunitconfig"    "$dst/.kunitconfig"

	local f
	for f in jockey3.c \
		 ploytec_proto.c ploytec_proto.h \
		 ploytec_codec.c ploytec_codec.h \
		 ploytec_midi.c  ploytec_midi.h \
		 ploytec_codec_kunit.c ploytec_codec_test_vectors.h; do
		cp "$SRC_DIR/$f" "$dst/$f"
	done
}

run_arch() {
	local arch=$1
	local build_dir=".kunit-$arch"
	local label="$arch"
	local prefix
	local -a args

	if [ "$use_reference_codec" -eq 1 ]; then
		build_dir="$build_dir-ref"
		label="$arch (portable reference codec)"
	fi

	args=(run "--kunitconfig=$DST_REL" "--build_dir=$build_dir")

	if [ "$arch" != um ]; then
		args+=("--arch=$arch")
		prefix=$(cross_prefix "$arch")
		[ -n "$prefix" ] && args+=("--cross_compile=$prefix")
	fi

	#
	# Two architectures cannot reach CONFIG_USB - and so cannot reach
	# sound/usb at all - without extra options. Both are added here rather
	# than in .kunitconfig, because kunit.py treats a requested option it
	# cannot satisfy as an error, so an arch-specific symbol in the shared
	# fragment breaks configuration on every *other* architecture.
	#
	case "$arch" in
	um)
		# UML defaults to NO_IOMEM. UML_PCI_OVER_VIRTIO selects UML_PCI,
		# which brings in the IOMEM emulation that makes USB_SUPPORT
		# selectable again. VIRTIO_UML is a hard dependency of it.
		args+=(--kconfig_add CONFIG_VIRTIO=y
		       --kconfig_add CONFIG_VIRTIO_UML=y
		       --kconfig_add CONFIG_UML_PCI_OVER_VIRTIO=y)
		;;
	s390)
		# arch/s390/Kconfig: "config HAS_IOMEM / def_bool PCI", so
		# without PCI there is no IOMEM, hence no SOUND either.
		args+=(--kconfig_add CONFIG_PCI=y)
		;;
	esac

	# The reference codec is behind EXPERT, so it needs enabling too.
	if [ "$use_reference_codec" -eq 1 ]; then
		args+=(--kconfig_add CONFIG_EXPERT=y
		       --kconfig_add CONFIG_SND_USB_JOCKEY3_REFERENCE_CODEC=y)
	fi

	echo
	echo "======================================================================"
	echo "== $label"
	echo "======================================================================"

	if ! target_ready "$arch"; then
		echo "  SKIPPED -- missing tools. Install with:"
		echo "      sudo apt install $(missing_for "$arch")"
		return 0
	fi

	( cd "$KUNIT_TREE" && ./tools/testing/kunit/kunit.py "${args[@]}" ) \
		|| { echo "  *** FAILED: $arch"; fails=$((fails + 1)); }
}

list_targets() {
	printf "%-8s %-26s %-24s %s\n" TARGET CROSS-COMPILER QEMU STATUS
	local arch prefix qemu status
	for arch in $DEFAULT_ARCHES $EXTRA_ARCHES; do
		prefix=$(cross_prefix "$arch")
		qemu=$(qemu_binary "$arch")
		if target_ready "$arch"; then
			status="ready"
		else
			status="missing: $(missing_for "$arch")"
		fi
		printf "%-8s %-26s %-24s %s\n" \
			"$arch" "${prefix:-(host)}" "${qemu:-(none, UML)}" "$status"
	done
}

arches=""
for arg in "$@"; do
	case "$arg" in
	--all)   arches="$arches $DEFAULT_ARCHES" ;;
	--extra) arches="$arches $EXTRA_ARCHES" ;;
	--ref)   use_reference_codec=1 ;;
	--list)  list_targets; exit 0 ;;
	--setup) setup_tree; echo "worktree ready at $KUNIT_TREE"; exit 0 ;;
	--clean)
		rm -rf "$KUNIT_TREE"/.kunit-*
		echo "removed build directories under $KUNIT_TREE"
		exit 0
		;;
	-h|--help)
		sed -n '2,36p' "$0"
		exit 0
		;;
	-*)
		echo "unknown option: $arg" >&2
		exit 1
		;;
	*)
		arches="$arches $arg"
		;;
	esac
done

[ -z "${arches// /}" ] && arches="um"

setup_tree

for arch in $arches; do
	run_arch "$arch"
done

echo
if [ "$fails" -eq 0 ]; then
	echo "All KUnit runs passed."
else
	echo "$fails KUnit run(s) FAILED."
fi
exit "$fails"
