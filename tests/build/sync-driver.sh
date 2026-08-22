#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Copy the driver sources -- and its kernel documentation -- into a kernel
# tree.
#
#   sync-driver.sh <kernel-tree>    copy the files
#   sync-driver.sh --list           print the mapping, one "src:dst" per line
#
# One file list, used by build_jockey3.sh (validation), build_kernel.sh (kernel
# packages) and use-committed.sh (the drift check). Two copies of this list
# would drift, and the failure mode is a build that silently omits a newly
# added source file -- or a check that silently fails to notice one changed.
#
# dst is a path relative to the kernel tree root, not to sound/usb/jockey3:
# every driver source lands under sound/usb/jockey3, but jockey3.rst lands
# under Documentation/sound/cards, so the mapping has to be able to say so.
# Three of the driver entries are also renamed on the way in: the kernel tree
# wants Makefile, the repository keeps Makefile.kernel so its own out-of-tree
# Makefile can coexist.
#
# Deliberately plain `cp -u`, never `cp -pu`: preserving mtimes makes the copy
# look older than the previous build's objects, so make skips the rebuild and
# silently revalidates the old revision. That has bitten this project twice.

set -eu

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

# src (in this repository) : dst (relative to the kernel tree root)
FILES=(
	"Kconfig:sound/usb/jockey3/Kconfig"
	"Makefile.kernel:sound/usb/jockey3/Makefile"
	".kunitconfig:sound/usb/jockey3/.kunitconfig"
	"jockey3.c:sound/usb/jockey3/jockey3.c"
	"ploytec_proto.c:sound/usb/jockey3/ploytec_proto.c"
	"ploytec_proto.h:sound/usb/jockey3/ploytec_proto.h"
	"ploytec_codec.c:sound/usb/jockey3/ploytec_codec.c"
	"ploytec_codec.h:sound/usb/jockey3/ploytec_codec.h"
	"ploytec_midi.c:sound/usb/jockey3/ploytec_midi.c"
	"ploytec_midi.h:sound/usb/jockey3/ploytec_midi.h"
	"ploytec_codec_kunit.c:sound/usb/jockey3/ploytec_codec_kunit.c"
	"ploytec_codec_test_vectors.h:sound/usb/jockey3/ploytec_codec_test_vectors.h"
	"ploytec_midi_kunit.c:sound/usb/jockey3/ploytec_midi_kunit.c"
	"Documentation/sound/cards/jockey3.rst:Documentation/sound/cards/jockey3.rst"
)

if [ "${1:-}" = "--list" ]; then
	printf '%s\n' "${FILES[@]}"
	exit 0
fi

TREE=${1:?usage: sync-driver.sh <kernel-tree> | --list}

for pair in "${FILES[@]}"; do
	src=${pair%%:*}
	dst=${pair#*:}
	mkdir -p "$TREE/$(dirname "$dst")"
	cp -uv "$SRC_DIR/$src" "$TREE/$dst"
done
