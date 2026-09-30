#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Gate every commit of an exported patch series on its own: each patch must
# build clean and pass the documentation and style checks with nothing after
# it applied.
#
#   gate-series.sh [options] [<kernel-range>]
#
#   <kernel-range>     commits to gate, in the kernel repository
#                      (default origin/for-next..jockey3-v5)
#   --variants <list>  builds per commit, comma-separated
#                      (default x86_64,x86_64-ref,i386,arm64,armhf)
#   --no-kunit         skip the KUnit runs
#   --only <n>         gate only the n-th commit of the range (1-based)
#
# Environment:
#   KERNEL_SRC    kernel repository the series is in       (default ~/sound)
#   SERIES_TREE   worktree the commits are checked out in  (default ~/sound-series)
#   SERIES_OUT    object trees and logs                    (default ~/kbuild-series)
#
# Why not build_jockey3.sh: that syncs from this repository's working tree,
# so run against an intermediate commit it would overwrite the state under
# test with the final driver and report green. Why not 'git rebase -x' in
# ~/sound: it needs the series branch checked out, which export-series.sh then
# refuses to move, and it leaves ~/sound on a state build_module.sh does not
# expect. So this keeps its own detached worktree, created on first use, and
# its own object trees -- nothing here touches ~/sound's checkout, ~/sound-build
# or ~/kbuild.
#
# Per commit, over whatever driver files that commit has:
#   build      make W=12 C=1 M=sound/usb/jockey3 for each variant, from a
#              clean module directory (an incremental build has reported zero
#              warnings having compiled nothing, and M= output lands in the
#              shared source tree, not the per-variant O= tree). The two known-benign
#              -Wshadow hits are filtered as in build_jockey3.sh, plus
#              sparse's own report of the same __ret shadow. A variant
#              is "<arch>" (tests/configs/<arch>-debug.config) or
#              "<arch>-ref" (the same plus the portable reference codec);
#              a -ref variant is n/a until the commit's Kconfig offers the
#              option. The default covers every target architecture, 32- and
#              64-bit x86 and ARM, because the optimized codec differs by word
#              size; a missing cross compiler is a failure, not a skip.
#   kdoc       kernel-doc -Wall -Werror over the .c and .h files
#   checkpatch --strict -g <commit>, which unlike a piped diff sees the
#              subject, sign-off and trailers
#   spell      codespell over the sources and the .rst
#   rst        rst2html --strict, once the commit has jockey3.rst
#   unused     every non-static ploytec_*() defined must be called somewhere
#              outside the KUnit tests -- a series must not add dead code,
#              and the compiler only warns for static functions
#   kunit      UML, once the commit has the KUnit suite; again with the
#              reference codec once that option exists
#
# Logs go to $SERIES_OUT/logs/<nn>-<sha>/. The exit status is the number of
# commits with a failure.

set -u

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)

KERNEL_SRC=${KERNEL_SRC:-$HOME/sound}
SERIES_TREE=${SERIES_TREE:-$HOME/sound-series}
SERIES_OUT=${SERIES_OUT:-$HOME/kbuild-series}
RANGE=origin/for-next..jockey3-v5
VARIANTS=x86_64,x86_64-ref,i386,arm64,armhf
KUNIT=1
ONLY=
DST=sound/usb/jockey3

usage() {
	sed -n '/^#   gate-series.sh/,/^# Environment/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2
	exit 2
}

range_set=0
while [ $# -gt 0 ]; do
	case $1 in
	--variants)	VARIANTS=${2:?}; shift 2 ;;
	--no-kunit)	KUNIT=0; shift ;;
	--only)		ONLY=${2:?}; shift 2 ;;
	-h|--help)	usage ;;
	-*)		echo "unknown option: $1" >&2; usage ;;
	*)		[ "$range_set" = 0 ] || usage; RANGE=$1; range_set=1; shift ;;
	esac
done

IFS=, read -r -a variants <<<"$VARIANTS"
for v in "${variants[@]}"; do
	[ -f "$REPO/tests/configs/${v%-ref}-debug.config" ] || {
		echo "unknown variant '$v': no tests/configs/${v%-ref}-debug.config" >&2; exit 2; }
done

mapfile -t commits < <(git -C "$KERNEL_SRC" rev-list --reverse "$RANGE") || exit 2
[ ${#commits[@]} -gt 0 ] || { echo "no commits in $RANGE" >&2; exit 2; }

if [ ! -d "$SERIES_TREE" ]; then
	echo "== creating worktree $SERIES_TREE =="
	git -C "$KERNEL_SRC" worktree add -q --detach "$SERIES_TREE" "${commits[0]}" || exit 2
fi
if [ -n "$(git -C "$SERIES_TREE" status --porcelain --untracked-files=no)" ]; then
	echo "$SERIES_TREE has local changes; it is scratch, but refusing to discard them" >&2
	exit 2
fi

T() { git -C "$SERIES_TREE" "$@"; }

# C=1 exits 0 having checked nothing if sparse is too old for the kernel's
# probe, so prove it is usable before trusting a clean result.
if [ "$(cd "$SERIES_TREE" && scripts/checker-valid.sh sparse 2>/dev/null)" != 1 ]; then
	echo "sparse is missing or too old for scripts/checker-valid.sh" >&2
	exit 2
fi

cross_for() {
	case $1 in
	x86_64|i386)	echo "x86 " ;;
	arm64)		echo "arm64 aarch64-linux-gnu-" ;;
	armhf)		echo "arm arm-linux-gnueabihf-" ;;
	esac
}

# Transient status on a terminal, as the hardware cases do.
status() { [ -t 1 ] && printf '\r\033[K%s' "$*"; }

# Per-commit results: "gate=verdict" words, verdict ok / FAIL / -
declare -a SUMMARY
failed_commits=0

run() {  # log command...  -> 0 if the command succeeded
	"${@:2}" >"$1" 2>&1
}

# An M= build writes its objects into the source directory, not the O= tree,
# so every variant shares one sound/usb/jockey3 in the worktree. Clean it
# before each build -- or one architecture links another's leftover objects
# -- and after the last, so the checks below see only the commit's sources.
clean_module() {
	git -C "$SERIES_TREE" clean -fdxq -- "$DST"
}

gate_build() {  # variant log -> sets $verdict
	local v=$1 log=$2 arch=${1%-ref} karch cross obj warns benign rc
	if [[ $v == *-ref ]] && ! grep -q '^config SND_USB_JOCKEY3_REFERENCE_CODEC' "$DST/Kconfig"; then
		verdict=-; return
	fi
	read -r karch cross <<<"$(cross_for "$arch")"
	if [ -n "$cross" ] && ! command -v "${cross}gcc" >/dev/null; then
		verdict=FAIL; echo "no ${cross}gcc" >"$log"; return
	fi
	obj=$SERIES_OUT/obj/$v
	mkdir -p "$obj"
	local -a mk=(-C "$SERIES_TREE" O="$obj" ARCH="$karch")
	[ -n "$cross" ] && mk+=(CROSS_COMPILE="$cross")

	# From the pristine config every time: olddefconfig drops a symbol the
	# commit's Kconfig does not define yet, and would not bring it back when
	# a later commit does.
	# Edited in place rather than appended: an appended duplicate makes
	# kconfig print a "reassigning" warning of its own.
	cp "$REPO/tests/configs/$arch-debug.config" "$obj/.config"
	[[ $v == *-ref ]] &&
		sed -i 's/^# CONFIG_SND_USB_JOCKEY3_REFERENCE_CODEC is not set$/CONFIG_SND_USB_JOCKEY3_REFERENCE_CODEC=y/' \
			"$obj/.config"
	{
		make "${mk[@]}" olddefconfig &&
		make -j"$(nproc)" "${mk[@]}" modules_prepare
	} >"${log%.log}-prepare.log" 2>&1 || { verdict=FAIL; return; }
	[[ $v != *-ref ]] ||
		grep -qx CONFIG_SND_USB_JOCKEY3_REFERENCE_CODEC=y "$obj/.config" ||
		{ echo "reference codec did not stick in .config" >"$log"; verdict=FAIL; return; }

	clean_module
	# modules_prepare leaves no full Module.symvers, so modpost's
	# unresolved-symbol errors are expected; see build_jockey3_arches.sh.
	KBUILD_MODPOST_WARN=1 make -j"$(nproc)" "${mk[@]}" M=$DST W=12 C=1 modules \
		>"$log" 2>&1
	rc=$?
	warns=$(grep -c -E '\bwarning:' "$log")
	benign=$(grep -E '\bwarning:' "$log" |
		grep -c -E "declaration of .index. shadows|declaration of .__ret. shadows|symbol .__ret. shadows an earlier one")
	if [ "$rc" -ne 0 ] || [ "$((warns - benign))" -gt 0 ]; then
		verdict=FAIL
	else
		verdict=ok
	fi
}

gate_unused() {  # log
	local f fn bad=0 users
	users=$(ls "$DST"/*.c | grep -v '_kunit\.c$')
	for f in "$DST"/ploytec_*.c; do
		[[ $f == *_kunit.c ]] && continue
		while read -r fn; do
			# a call is "fn(" on a line that is neither the definition
			# nor inside a comment block
			# shellcheck disable=SC2086
			grep -h -E "\\b$fn\\(" $users |
				grep -v -E "^[a-z].* \\**$fn\\(|^[[:space:]]*\\*" | grep -q . ||
				{ echo "$f: $fn() has no caller"; bad=1; }
		done < <(grep -oE '^[a-z][a-z0-9_ *]* \**(ploytec_[a-z0-9_]+)\(' "$f" |
			 grep -v '^static' | grep -oE 'ploytec_[a-z0-9_]+\($' | tr -d '(' | sort -u)
	done >"$1"
	return $bad
}

gate_kunit() {  # ref log
	local -a args=(run "--kunitconfig=$DST" "--build_dir=$SERIES_OUT/kunit-um$1"
		       --kconfig_add CONFIG_VIRTIO=y --kconfig_add CONFIG_VIRTIO_UML=y
		       --kconfig_add CONFIG_UML_PCI_OVER_VIRTIO=y)
	[ -n "$1" ] && args+=(--kconfig_add CONFIG_EXPERT=y
			      --kconfig_add CONFIG_SND_USB_JOCKEY3_REFERENCE_CODEC=y)
	./tools/testing/kunit/kunit.py "${args[@]}" >"$2" 2>&1
}

n=0
for c in "${commits[@]}"; do
	n=$((n + 1))
	[ -z "$ONLY" ] || [ "$ONLY" = "$n" ] || continue
	short=$(git -C "$KERNEL_SRC" rev-parse --short "$c")
	subject=$(git -C "$KERNEL_SRC" log -1 --format=%s "$c")
	logs=$SERIES_OUT/logs/$(printf %02d "$n")-$short
	rm -rf "$logs"; mkdir -p "$logs"
	line=()
	fail=0
	verdict() {  # name verdict
		line+=("$1=$2"); [ "$2" != FAIL ] || fail=1
	}

	T checkout -q --detach "$c" || { echo "cannot check out $short" >&2; exit 2; }
	cd "$SERIES_TREE" || exit 2

	for v in "${variants[@]}"; do
		status "$(printf '%2d/%d %s  build %s' "$n" "${#commits[@]}" "$short" "$v")"
		gate_build "$v" "$logs/build-$v.log"
		verdict "$v" "$verdict"
	done

	clean_module
	status "$(printf '%2d/%d %s  checks' "$n" "${#commits[@]}" "$short")"
	srcs=("$DST"/*.c "$DST"/*.h)
	run "$logs/kdoc.log" sh -c 'for f; do scripts/kernel-doc -Wall -Werror --none "$f" || exit 1; done' - \
		"${srcs[@]}" && verdict kdoc ok || verdict kdoc FAIL

	run "$logs/checkpatch.log" scripts/checkpatch.pl --strict --ignore FILE_PATH_CHANGES -g "$c" \
		&& verdict checkpatch ok || verdict checkpatch FAIL

	rst=Documentation/sound/cards/jockey3.rst
	docs=(); [ -f "$rst" ] && docs=("$rst")
	run "$logs/spell.log" codespell "${srcs[@]}" "${docs[@]}" \
		&& verdict spell ok || verdict spell FAIL
	if [ -f "$rst" ]; then
		run "$logs/rst.log" rst2html --strict "$rst" && verdict rst ok || verdict rst FAIL
	else
		verdict rst -
	fi

	gate_unused "$logs/unused.log" && verdict unused ok || verdict unused FAIL

	if [ "$KUNIT" = 0 ] || [ ! -f "$DST/ploytec_codec_kunit.c" ]; then
		verdict kunit -
	else
		status "$(printf '%2d/%d %s  kunit' "$n" "${#commits[@]}" "$short")"
		gate_kunit "" "$logs/kunit.log" && verdict kunit ok || verdict kunit FAIL
		if grep -q '^config SND_USB_JOCKEY3_REFERENCE_CODEC' "$DST/Kconfig"; then
			gate_kunit -ref "$logs/kunit-ref.log" && verdict kunit-ref ok ||
				verdict kunit-ref FAIL
		fi
	fi

	cd - >/dev/null || exit 2
	status ""
	[ "$fail" = 0 ] || failed_commits=$((failed_commits + 1))
	printf '%2d %s %-4s %s\n      %s\n' "$n" "$short" "$([ "$fail" = 0 ] && echo ok || echo FAIL)" \
		"$subject" "${line[*]}"
	SUMMARY+=("$n")
done

echo
echo "logs: $SERIES_OUT/logs/"
if [ "$failed_commits" -eq 0 ]; then
	echo "All ${#SUMMARY[@]} commit(s) passed."
else
	echo "$failed_commits of ${#SUMMARY[@]} commit(s) FAILED."
fi
exit "$failed_commits"
