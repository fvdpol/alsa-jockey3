#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Line up a rate_stall_trace.bt log against a run's dmesg.txt/JT-MARKs.

Both bpftrace's `nsecs` and the kernel log's `[nnnn.nnnnnn]` timestamps are
boot-relative monotonic time (ktime_get_ns()), so a trace line and a dmesg
line are directly comparable once both are read as seconds -- no clock
translation needed. This script does that lookup so it does not have to be
re-derived by hand from a `python3 -c` one-liner each time, which is how the
first pass at this (2026-08-26, the N=8 rate-stall race finding in
re/rate_change_stall.md) was done.

Two things it answers:

  onsets   -- for every "watchdog_onset"/"URB has stalled" line in dmesg.txt,
              how long after the preceding JT-MARK #changeN marker did it
              fire? Prints the distribution (n, median, IQR). This is what
              established that stall onset is a fixed ~275-360ms after the
              change marker, not something that develops mid-stream (see
              re/rate_change_stall.md's stall-timing section).

  window   -- dump every trace_log line (and, with --with-dmesg, every
              dmesg.txt line) inside [center - before, center + after] of a
              given boot-relative timestamp, e.g. the timestamp of a
              specific stall onset from the `onsets` mode. This is what
              found the SET_RATE -> START_URBS -> clean completions -> ~1ms
              later the watchdog's own redundant restart -> reset sequence.

Usage:
    python3 re/bpftrace/correlate_trace.py onsets RUN_DIR
    python3 re/bpftrace/correlate_trace.py window RUN_DIR CENTER_S \
        [--before 0.05] [--after 0.3] [--with-dmesg]

RUN_DIR is a results/<target>/<run-id>/ directory containing dmesg.txt and
(for `window`) rate_stall_trace.log.
"""
import argparse
import os
import re
import sys

MARK_RE = re.compile(r"\[(\d+\.\d+)\] JT-MARK \S+#change(\d+)-\d+")
# The two stall-onset detectors classify_events() in cases/rate_change.py
# distinguishes as watchdog_onset and the STALL_LINE check; both are of
# interest for "how long after the change did the first sign of trouble
# appear", so both are matched here.
ONSET_RE = re.compile(
    r"\[(\d+\.\d+)\] snd-reloop-jockey3.*?(Playback|Capture) "
    r"(?:URB stream stalled: no completion for \d+ ms|URB has stalled\.)")
TS_RE = re.compile(r"ts_ns=(\d+)")


def read_lines(path):
    return open(path, errors="replace").read().splitlines()


def percentile(sorted_vals, p):
    n = len(sorted_vals)
    return sorted_vals[min(n - 1, int(n * p))]


def cmd_onsets(run_dir, _args):
    dmesg = read_lines(os.path.join(run_dir, "dmesg.txt"))
    marks = []
    for line in dmesg:
        m = MARK_RE.search(line)
        if m:
            marks.append(float(m.group(1)))
    marks.sort()
    if not marks:
        print("no JT-MARK #changeN markers found in dmesg.txt", file=sys.stderr)
        return 1

    deltas = []
    import bisect
    for line in dmesg:
        o = ONSET_RE.search(line)
        if not o:
            continue
        t = float(o.group(1))
        i = bisect.bisect_right(marks, t) - 1
        if i < 0:
            continue
        deltas.append((t - marks[i]) * 1000.0)

    if not deltas:
        print("no stall-onset lines found", file=sys.stderr)
        return 0
    deltas.sort()
    n = len(deltas)
    print(f"n={n}  min={deltas[0]:.1f}ms  p25={percentile(deltas, 0.25):.1f}ms "
          f" median={percentile(deltas, 0.5):.1f}ms  "
          f"p75={percentile(deltas, 0.75):.1f}ms  max={deltas[-1]:.1f}ms")
    return 0


def cmd_window(run_dir, args):
    # --center is given in dmesg's units (seconds) for readability against
    # a [nnnn.nnnnnn] dmesg line; rate_stall_trace.bt's ts_ns is
    # nanoseconds, so the trace-side bounds need scaling up.
    lo = (args.center - args.before) * 1e9
    hi = (args.center + args.after) * 1e9

    trace_path = os.path.join(run_dir, "rate_stall_trace.log")
    if os.path.exists(trace_path):
        for line in read_lines(trace_path):
            m = TS_RE.search(line)
            if m and lo <= int(m.group(1)) <= hi:
                print(line)
    else:
        print(f"(no {trace_path} -- skipping trace lines)", file=sys.stderr)

    if args.with_dmesg:
        dmesg_lo, dmesg_hi = args.center - args.before, args.center + args.after
        for line in read_lines(os.path.join(run_dir, "dmesg.txt")):
            m = re.match(r"\[(\d+\.\d+)\]", line)
            if m and dmesg_lo <= float(m.group(1)) <= dmesg_hi:
                print(line)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_onsets = sub.add_parser("onsets", help="stall-onset delay distribution")
    p_onsets.add_argument("run_dir")

    p_window = sub.add_parser("window", help="dump trace/dmesg around a timestamp")
    p_window.add_argument("run_dir")
    p_window.add_argument("center", type=float,
                          help="boot-relative seconds, e.g. from dmesg.txt's "
                               "[nnnn.nnnnnn] or an `onsets` result")
    p_window.add_argument("--before", type=float, default=0.05)
    p_window.add_argument("--after", type=float, default=0.3)
    p_window.add_argument("--with-dmesg", action="store_true")

    args = ap.parse_args()
    if args.cmd == "onsets":
        return cmd_onsets(args.run_dir, args)
    return cmd_window(args.run_dir, args)


if __name__ == "__main__":
    sys.exit(main())
