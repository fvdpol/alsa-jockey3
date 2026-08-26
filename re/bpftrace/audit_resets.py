#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Recompute JT-RATE-00{1,2,3} resets_total_device from raw dmesg.txt and
diff it against the stored result.json, instead of trusting the metric.

Written 2026-08-26 after finding that cases/rate_change.py's classify_events()
was missing the watchdog's own reset-escalation context ("queuing full USB
reset (watchdog)", jockey3.c:1744, added 2026-08-23) from its CONTEXT list.
Every reset queued that way logged normally but was invisible to
resets_total_device/resets_per_change_pct and, via branch_of(), misclassified
as a self-recovered "deferred" stall. See re/rate_change_stall.md's
2026-08-26 follow-up for the full story -- the bug is now fixed in
cases/rate_change.py, but every run recorded between the watchdog gaining
that path and the fix landing needs this script (or a re-run) to know its
true reset count. Keep this script rather than re-deriving it: the next test-
framework counting gap will not announce itself, and this is exactly the
audit that found the last one.

Each run's dmesg.txt holds the whole kernel ring buffer's worth of history,
not just this run's -- so the window MUST be bounded by that run's own
JT-MARK markers (project convention: the file's own comment in
tests/hw/runner.py documents dmesg.txt as ring-buffer-bounded, not
run-bounded). Whole-file grep is the mirror image of the bug this script
exists to find.

Usage:
    python3 re/bpftrace/audit_resets.py [results-root]

results-root defaults to tests/hw/results relative to the repo checkout this
script lives in. Only reports cases where the raw-log truth disagrees with
the stored metric.
"""
import glob
import json
import os
import re
import sys

# tests/hw/cases/ needs to be importable to reuse classify_events()/
# count_events() verbatim rather than re-deriving the driver's log-message
# regexes a second time somewhere they can drift out of sync.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO_ROOT, "tests", "hw", "cases"))
import rate_change as rc  # noqa: E402

RESET_CONTEXTS = (
    "reset_on_rate_change",
    "reset_after_urb_restart",
    "reset_after_playback_prepare",
    "reset_on_watchdog",
)


def true_resets_for_case(dmesg_lines, case_id):
    """Bound dmesg_lines to case_id's own JT-MARK span, then classify it.

    Returns (resets, first_marker_idx) or (None, None) if this case's
    markers are not present in this dmesg.txt at all.
    """
    marker_re = re.compile(re.escape(case_id) + r"#")
    first_idx = last_idx = None
    for i, line in enumerate(dmesg_lines):
        if "JT-MARK" in line and marker_re.search(line):
            if first_idx is None:
                first_idx = i
            last_idx = i
    if first_idx is None:
        return None, None
    window = dmesg_lines[first_idx:last_idx + 1]
    counts = rc.count_events(rc.classify_events(window))
    resets = sum(counts.get(k, 0) for k in RESET_CONTEXTS)
    return resets, counts


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        _REPO_ROOT, "tests", "hw", "results")
    mismatches = 0
    checked = 0
    for dmesg_path in sorted(glob.glob(os.path.join(root, "*", "*", "dmesg.txt"))):
        run_dir = os.path.dirname(dmesg_path)
        dmesg_lines = open(dmesg_path, errors="replace").read().splitlines()
        for case_dir in sorted(glob.glob(os.path.join(run_dir, "cases", "JT-RATE-*"))):
            result_path = os.path.join(case_dir, "result.json")
            if not os.path.exists(result_path):
                continue
            m = re.match(r"(JT-RATE-\d+)", os.path.basename(case_dir))
            if not m:
                continue
            case_id = m.group(1)
            true_resets, counts = true_resets_for_case(dmesg_lines, case_id)
            if true_resets is None:
                continue
            try:
                result = json.load(open(result_path))
            except (OSError, json.JSONDecodeError):
                continue
            reported = result.get("metrics", {}).get("resets_total_device")
            if reported is None:
                continue
            checked += 1
            if reported != true_resets:
                mismatches += 1
                rel_run = os.path.relpath(run_dir, root)
                print(f"{rel_run:50s} {case_id:12s} "
                      f"reported={reported} true={true_resets}  {counts}")
    print(f"\n{mismatches} mismatches out of {checked} case results checked",
          file=sys.stderr)
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
