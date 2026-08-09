#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Copy the driver sources into a kernel tree's sound/usb/jockey3/.
#
#   sync-driver.sh <kernel-tree>
#
# One file list, used by both build_jockey3.sh (validation) and
# build_kernel.sh (kernel packages). Two copies of this list would drift, and
# the failure mode is a build that silently omits a new source file.
#
# Deliberately plain `cp -u`, never `cp -pu`: preserving mtimes makes the copy
# look older than the previous build's objects, so make skips the rebuild and
# silently revalidates the old revision. That has bitten this project twice.

set -eu

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
TREE=${1:?usage: sync-driver.sh <kernel-tree>}
DST=$TREE/sound/usb/jockey3

FILES=(
	jockey3.c
	ploytec_proto.c ploytec_proto.h
	ploytec_codec.c ploytec_codec.h
	ploytec_midi.c  ploytec_midi.h
	ploytec_codec_kunit.c ploytec_codec_test_vectors.h
)

mkdir -p "$DST"
cp -uv "$SRC_DIR/Kconfig"         "$DST/Kconfig"
cp -uv "$SRC_DIR/Makefile.kernel" "$DST/Makefile"
cp -uv "$SRC_DIR/.kunitconfig"    "$DST/.kunitconfig"
for f in "${FILES[@]}"; do
	cp -uv "$SRC_DIR/$f" "$DST/$f"
done
