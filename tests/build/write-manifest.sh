#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Record which driver revision produced a module binary.
#
#   write-manifest.sh <module.ko> [kernel-tree] [kernel-release]
#
# The kernel release is passed in for O= builds, where the source tree holds no
# .config and `make kernelrelease` therefore answers nothing useful.
#
# Writes <results-root>/manifests/<build-id>.json. A test run reads the
# build-id back from /sys/module/snd_reloop_jockey3/notes/ and looks it up, so
# a result can name the exact commit it tested -- without adding a version
# string, an export or a sysfs attribute to the driver for the benefit of
# tests.
#
# Keyed by GNU build-id rather than srcversion. srcversion is only emitted when
# the kernel is built with CONFIG_MODULE_SRCVERSION_ALL, which neither Debian
# nor the Raspberry Pi kernels set -- so it is missing on exactly the machines
# this has to work on. The build-id is an ELF note the linker always writes,
# and the kernel exposes module notes in sysfs everywhere. srcversion is
# recorded too when present, as a source-level cross-check: two builds of
# identical source share a srcversion but not a build-id.
#
# Environment:
#   JOCKEY3_RESULTS_DIR    shared results root (default tests/hw/results)
#   JOCKEY3_MANIFEST_DIR   override the manifest directory outright

set -eu

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
KO=${1:?usage: write-manifest.sh <module.ko> [kernel-tree] [kernel-release]}
KERNEL_SRC=${2:-${KERNEL_SRC:-$HOME/sound}}
KERNEL_RELEASE=${3:-}
[ -n "$KERNEL_RELEASE" ] || KERNEL_RELEASE=$(
	make -s -C "$KERNEL_SRC" kernelrelease 2>/dev/null | tail -1)
MANIFEST_DIR=${JOCKEY3_MANIFEST_DIR:-${JOCKEY3_RESULTS_DIR:-$SRC_DIR/tests/hw/results}/manifests}

[ -f "$KO" ] || { echo "no module at $KO" >&2; exit 2; }

# readelf is the simple route; the Python fallback exists because a build host
# may have neither binutils' readelf nor kmod -- it never loads modules.
read_build_id() {
	local ko=$1 v=""
	if command -v readelf >/dev/null 2>&1; then
		v=$(readelf -n "$ko" 2>/dev/null \
			| sed -n 's/.*Build ID: \([0-9a-f]*\).*/\1/p' | head -1)
	fi
	if [ -z "$v" ] && command -v python3 >/dev/null 2>&1; then
		v=$(python3 - "$ko" <<-'PY'
		import struct, sys
		with open(sys.argv[1], "rb") as f:
		    d = f.read()
		if d[:4] != b"\x7fELF" or d[4] != 2:
		    sys.exit(0)
		e_shoff, = struct.unpack_from("<Q", d, 0x28)
		e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", d, 0x3a)
		base = e_shoff + e_shstrndx * e_shentsize
		str_off, str_size = struct.unpack_from("<QQ", d, base + 0x18)
		names = d[str_off:str_off + str_size]
		for i in range(e_shnum):
		    sh = e_shoff + i * e_shentsize
		    nameoff, = struct.unpack_from("<I", d, sh)
		    name = names[nameoff:names.index(b"\0", nameoff)].decode()
		    if name != ".note.gnu.build-id":
		        continue
		    off, size = struct.unpack_from("<QQ", d, sh + 0x18)
		    note = d[off:off + size]
		    namesz, descsz, _ = struct.unpack_from("<III", note, 0)
		    p = 12 + (namesz + 3) // 4 * 4
		    print(note[p:p + descsz].hex())
		    break
		PY
		)
	fi
	printf '%s' "$v"
}

build_id=$(read_build_id "$KO")
if [ -z "$build_id" ]; then
	echo "  (no build-id in $KO -- test runs will not be able to resolve" >&2
	echo "   the loaded module to a git revision)" >&2
	exit 1
fi

srcversion=$(grep -a -o 'srcversion=[0-9A-Fa-f]\{16,\}' "$KO" 2>/dev/null \
	| head -1 | cut -d= -f2 || true)

# Both worktree and index: `git diff` alone reports clean when changes are
# merely staged, which would label a modified build as pristine.
dirty=false
[ -z "$(git -C "$SRC_DIR" status --porcelain 2>/dev/null)" ] || dirty=true

mkdir -p "$MANIFEST_DIR"
cat > "$MANIFEST_DIR/$build_id.json" <<EOF
{
  "build_id": "$build_id",
  "srcversion": "${srcversion:-}",
  "module": "$(basename "$KO")",
  "git_hash": "$(git -C "$SRC_DIR" rev-parse HEAD 2>/dev/null)",
  "git_branch": "$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)",
  "git_describe": "$(git -C "$SRC_DIR" describe --always --dirty 2>/dev/null)",
  "dirty": $dirty,
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "built_on": "$(uname -n)",
  "kernel_src": "$KERNEL_SRC",
  "kernel_release": "$KERNEL_RELEASE",
  "arch": "$(uname -m)"
}
EOF
echo "manifest: $MANIFEST_DIR/$build_id.json"
