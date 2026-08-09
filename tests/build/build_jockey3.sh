#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Build and check the Jockey 3 driver in a kernel tree.
#
# Sources are edited in this repository and copied into the kernel tree, so
# this script is the single point where the staging tree is refreshed and
# validated. It is also where the build manifest is written, which is what
# lets a test run resolve the loaded module back to a git revision.
#
# Usage:
#   bash build_jockey3.sh                 all gates
#   bash build_jockey3.sh --gate docs     one gate: sync docs build checkpatch size
#   DOCS=1 bash build_jockey3.sh          also run the full Sphinx build
#   BASE=<ref> bash build_jockey3.sh      checkpatch against a different base
#   JSON_REPORT=<path> bash ...           write a machine-readable report
#
# Environment:
#   KERNEL_SRC   kernel tree to build in       (default ~/sound)
#   BASE         checkpatch base ref           (default origin/for-next)
#   JOCKEY3_RESULTS_DIR  shared results root; manifests go in its manifests/
#
# The default checkpatch base is origin/for-next, i.e. everything this tree
# adds on top of upstream. That is stable across rebases and squashes, unlike
# a HEAD~n offset, and it is exactly the diff that will become the patch
# series.
#
# This script depends on the local layout of the kernel tree, which is why it
# lived outside version control for a long time. That dependence is expressed
# through KERNEL_SRC rather than paid for by leaving the script unversioned:
# an unversioned build gate cannot be reviewed, and its behavior cannot be
# correlated with the results it produced.

set -u

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
KERNEL_SRC=${KERNEL_SRC:-$HOME/sound}
BASE=${BASE:-origin/for-next}
# Manifests go under the results root, not into the repository: the module is
# built here and run on another machine, so the manifest has to travel by the
# same shared directory the results do.
MANIFEST_DIR=${JOCKEY3_MANIFEST_DIR:-${JOCKEY3_RESULTS_DIR:-$SRC_DIR/tests/hw/results}/manifests}
DST=sound/usb/jockey3
GATE=all

while [ $# -gt 0 ]; do
	case "$1" in
	--gate) GATE=$2; shift 2 ;;
	--help|-h)
		sed -n '3,30p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
		exit 0 ;;
	*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done

cd "$KERNEL_SRC" || { echo "no kernel tree at $KERNEL_SRC" >&2; exit 2; }

fails=0
declare -a GATE_NAMES GATE_OK GATE_DETAIL
declare -A METRICS

fail() { echo "  *** FAILED: $1"; fails=$((fails + 1)); }

record() {  # name ok detail
	GATE_NAMES+=("$1"); GATE_OK+=("$2"); GATE_DETAIL+=("$3")
}

want() { [ "$GATE" = all ] || [ "$GATE" = "$1" ]; }

# ---------------------------------------------------------------- sync sources
# Always runs: every other gate needs the staging tree to be current, and a
# gate that validates stale sources is worse than no gate at all.
#
# The file list lives in sync-driver.sh, shared with build_kernel.sh. Two
# copies would drift, and the failure mode is a build that silently omits a
# newly added source file.
"$SRC_DIR/tests/build/sync-driver.sh" "$KERNEL_SRC"

# ------------------------------------------------------------------ docs gate
# kernel-doc, codespell and rst, grouped: they are all "is the documentation
# well-formed", they are all fast, and they fail for the same kind of reason.
if want docs; then
	before=$fails
	echo "== kernel-doc =="
	for f in $DST/*.c; do
		./scripts/kernel-doc -Wall -Werror --none "$f" || fail "kernel-doc $f"
	done

	echo "== codespell =="
	if command -v codespell >/dev/null 2>&1; then
		codespell $DST/*.c $DST/*.h \
			Documentation/sound/cards/jockey3.rst || fail "codespell"
	else
		echo "  (codespell not installed, skipped)"
	fi

	# Fast standalone syntax check; the authoritative run is the Sphinx
	# build below.
	echo "== rst syntax =="
	if command -v rst2html >/dev/null 2>&1; then
		rst2html --strict Documentation/sound/cards/jockey3.rst >/dev/null \
			|| fail "rst2html jockey3.rst"
	else
		echo "  (rst2html not installed -- apt-get install python3-docutils)"
	fi

	[ "$fails" -eq "$before" ] && record docs true "" \
		|| record docs false "documentation gate failed"
fi

# ----------------------------------------------------------------- build gate
# W=1: relevant warnings that are useful but omitted by default.
# W=2: noisier warnings that trigger often but can still find real bugs.
# W=3: hyper-strict, highly pedantic warnings.
# W=c: checkpatch and external static analysis during compilation.
# W=e: treat all warnings as errors.
#
# Two -Wshadow warnings are expected and are not defects:
#   jockey3.c: 'index' shadows a built-in -- the standard ALSA module-param
#              idiom, used by every card driver.
#   wait.h:    '__ret' shadows -- internal to wait_event_lock_irq_timeout();
#              every in-tree user hits it at W=2.
if want build || want size; then
	echo "== build =="
	buildlog=$(mktemp)
	make -j"$(nproc)" LOCALVERSION=-alsa-dev W=12 M=$DST 2>&1 | tee "$buildlog"
	rc=${PIPESTATUS[0]}

	# grep -c prints a count AND exits 1 when there are no matches, so a
	# `|| echo 0` fallback would append a second line and break the
	# arithmetic below. Take the count and default only if it is empty.
	warns=$(grep -c -E '\bwarning:' "$buildlog" 2>/dev/null)
	# Filter the two known-benign shadow warnings so the count means
	# something. Silencing them in the source would be worse: they are
	# correct observations about idiomatic code.
	benign=$(grep -c -E "shadowed declaration|declaration of 'index' shadows|'__ret' shadows" \
		"$buildlog" 2>/dev/null)
	warns=${warns:-0}
	benign=${benign:-0}
	METRICS[build_warnings]=$(( warns > benign ? warns - benign : 0 ))

	if want build; then
		if [ "$rc" -ne 0 ]; then
			fail "make"
			record build false "make exited $rc"
		elif [ "${METRICS[build_warnings]}" -ne 0 ]; then
			fail "make: ${METRICS[build_warnings]} warning(s) at W=12"
			record build false "${METRICS[build_warnings]} warnings"
		else
			record build true ""
		fi
	fi
	rm -f "$buildlog"
fi

# ------------------------------------------------------------------ size gate
# Metric only -- never fails. Answers "why did it grow fat?". A threshold here
# would either be too loose to catch anything or would block a real feature.
if want size; then
	KO=$DST/snd-reloop-jockey3.ko
	if [ -f "$KO" ]; then
		METRICS[ko_bytes]=$(stat -c %s "$KO")
		if command -v size >/dev/null 2>&1; then
			read -r t d b _ < <(size -B "$KO" | tail -1)
			METRICS[text_bytes]=$t
			METRICS[data_bytes]=$d
			METRICS[bss_bytes]=$b
		fi
		echo "== size =="
		echo "  ${KO##*/}: ${METRICS[ko_bytes]} bytes" \
		     "(text ${METRICS[text_bytes]:-?} data ${METRICS[data_bytes]:-?})"
		record size true ""
	else
		record size false "no module built"
		fail "size: $KO not found"
	fi
fi

# ------------------------------------------------------------ checkpatch gate
# Covers the driver, its documentation and the MAINTAINERS entry.
#
# FILE_PATH_CHANGES is ignored: checkpatch raises it on the first added file in
# any patch and then suppresses itself, without ever checking whether
# MAINTAINERS was actually touched. This series does update MAINTAINERS, so it
# is a false positive that would otherwise fire on every run.
# --codespell is deliberately not passed: codespell already ran standalone
# above, over the same files, and running it again here roughly doubles
# checkpatch's runtime for no extra coverage.
if want checkpatch; then
	echo "== checkpatch ($BASE..HEAD) =="
	if git rev-parse --verify --quiet "$BASE" >/dev/null; then
		cplog=$(mktemp)
		git diff "$BASE" -- $DST Documentation/sound/cards MAINTAINERS \
			| ./scripts/checkpatch.pl --strict \
				--ignore FILE_PATH_CHANGES - 2>&1 | tee "$cplog"
		rc=${PIPESTATUS[1]}
		cpw=$(grep -c '^WARNING:' "$cplog" 2>/dev/null)
		cpe=$(grep -c '^ERROR:' "$cplog" 2>/dev/null)
		METRICS[checkpatch_warnings]=${cpw:-0}
		METRICS[checkpatch_errors]=${cpe:-0}
		rm -f "$cplog"
		if [ "$rc" -ne 0 ]; then
			fail "checkpatch"
			record checkpatch false \
				"${METRICS[checkpatch_errors]} errors, ${METRICS[checkpatch_warnings]} warnings"
		else
			record checkpatch true ""
		fi
	else
		echo "  (base '$BASE' does not resolve -- rerun with BASE=<ref>)"
		fails=$((fails + 1))
		record checkpatch false "base '$BASE' does not resolve"
	fi
fi

# --------------------------------------------------------------- documentation
# Slower, so opt-in. Catches toctree and cross-reference problems that the
# standalone rst2html check cannot see.
if [ -n "${DOCS:-}" ] && want docs; then
	echo "== htmldocs =="
	make SPHINXDIRS="sound" htmldocs || fail "htmldocs"
	echo "  output: Documentation/output/sound/cards/jockey3.html"
fi

# ------------------------------------------------------------- build manifest
# Records which driver revision produced this module, so a test run on another
# machine can name the commit it tested. Shared with build_kernel.sh.
KO=$DST/snd-reloop-jockey3.ko
[ -f "$KO" ] && "$SRC_DIR/tests/build/write-manifest.sh" "$KO" "$KERNEL_SRC" || true

# ---------------------------------------------------------------- JSON report
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

# ------------------------------------------------------------------- summary
echo
if [ "$fails" -eq 0 ]; then
	echo "All checks passed."
else
	echo "$fails check(s) FAILED."
fi
exit "$fails"
