#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""issue48 -- sweep ploytec_proto.c's issue48_settle_us module param against
real device power cycles, to find the settle delay a post-enumeration
usb_clear_halt() race actually needs.

NEVER MERGE, NEVER RUN except on the test/issue48-ep0-set-interface-trace
branch's instrumented build. See github.com/fvdpol/alsa-jockey3/issues/48.

    issue48_settle_sweep.py <path-to-ko> [--values 0,2000,5000,7500,15000]
                             [--cycles 10] [--off-seconds 5] [--timeout 30]
                             [--out results.json] [--save-trace DIR]

For each candidate value: reload the module with issue48_settle_us=<value>,
then run --cycles real device power cycles (lib.power, same device-power
actuator JT-AUDIO-005 uses), and for each cycle count how many enumeration
attempts it took before a clean "Firmware ... vN.N.N" line -- 1 means no
flapping at all, N>1 means N-1 failed probes before it settled. That is the
same "how many re-enumerations to stabilize" question the manual power-toggle
sessions in issue #48 answered by hand, just parameterized and repeated.

THIS SCRIPT ASKS FOR A PASSWORD, REPEATEDLY, AND THAT IS DELIBERATE
---------------------------------------------------------------------
Changing a module's load-time parameter means unloading and reinserting it,
which is arbitrary kernel code same as any other module install -- see
reload_driver.sh's header for why that is never handed to a sudoers rule.
This only reinserts the one .ko passed on the command line, at a parameter
swept across a fixed, printed list of values, never something read back from
the device or the network.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib import kmsg, power                # noqa: E402

MODULE = "snd_reloop_jockey3"
FAIL_MARKER = "probe with driver snd-reloop-jockey3 failed with error"
OK_MARKER = "Reloop Jockey 3 Remix Firmware"
TRACE_PATH = "/sys/kernel/debug/tracing/trace"
KMSG_TS = re.compile(r"\[\s*(\d+\.\d+)\]")


def reload_with(ko_path, value, range_us, trace_enabled):
    """value is an int (issue48_settle_us) or the literal string
    "cond_resched" (issue48_cond_resched=1, issue48_settle_us=0)."""
    # trace_enabled off by default: a settle_us=0 sweep measured a far lower
    # flap rate than the uninstrumented driver with tracing always on --
    # trace_printk()'s own small per-call cost was apparently enough to
    # shift the odds. Only pay that cost (and only get the detail) when
    # --save-trace actually asked for it.
    settle_us = 0 if value == "cond_resched" else value
    cond_resched = 1 if value == "cond_resched" else 0
    subprocess.run(["sudo", "rmmod", MODULE], check=False,
                    capture_output=True)
    r = subprocess.run(
        ["sudo", "insmod", ko_path, f"issue48_settle_us={settle_us}",
         f"issue48_settle_range_us={range_us}",
         f"issue48_cond_resched={cond_resched}",
         f"issue48_trace_enabled={1 if trace_enabled else 0}"])
    if r.returncode != 0:
        sys.exit(f"insmod failed for value={value}")


def clear_trace():
    """Reset ftrace's ring buffer so each cycle's saved trace is just its own.

    Same reasoning as run_marker.write() bounding a run's dmesg.txt elsewhere
    in this suite: without this, a later cycle's saved trace also contains
    every earlier cycle still sitting in the buffer.
    """
    subprocess.run(["sudo", "tee", TRACE_PATH], input="", text=True,
                    capture_output=True, check=False)
    subprocess.run(["sudo", "sh", "-c", f"echo 1 > {TRACE_PATH.rsplit('/', 1)[0]}/tracing_on"],
                    check=False, capture_output=True)


def save_trace(dest):
    """Returns the trace text (also written to `dest`), or None on failure."""
    r = subprocess.run(["sudo", "cat", TRACE_PATH], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    with open(dest, "w", encoding="utf-8") as f:
        f.write(r.stdout)
    return r.stdout


# ftrace's own format, not kmsg's -- see the header comment on ISSUE48_TRACE:
#      kworker/0:3-3831    [000] .....  8691.171423: ploytec_initialize_device: issue48 1-1.3.2:1.0: get_firmware start
TRACE_LINE = re.compile(r"^\s*\S+\s+\[\d+\]\s+\S+\s+(\d+\.\d+):\s+\S+:\s+issue48\s+\S+:\s*(.*)$")


def measure_delays(trace_text, start_msg, end_msg):
    """Actual elapsed microseconds between each start_msg/end_msg pair in a
    saved trace -- e.g. "settle N us start"/"settle done" (versus the
    requested issue48_settle_us: usleep_range()'s underlying hrtimer is
    precise regardless of CONFIG_HZ, CONFIG_HIGH_RES_TIMERS=y on this
    target, but that only covers when the timer fires, not when the
    sleeping task actually gets the CPU back; real wakeup latency can run
    well past what was requested) or "cond_resched start"/"cond_resched
    done" (where there is no requested duration to compare against at
    all -- this just reports how long the reschedule actually took).
    """
    events = []
    for line in trace_text.splitlines():
        m = TRACE_LINE.match(line)
        if m:
            events.append((float(m.group(1)), m.group(2)))
    delays = []
    for i, (ts, msg) in enumerate(events):
        if msg == start_msg:
            for ts2, msg2 in events[i + 1:]:
                if msg2 == end_msg:
                    delays.append(round((ts2 - ts) * 1_000_000))
                    break
    return delays


def lines_since(marker):
    """Kernel log lines written after `marker` was written, marker excluded."""
    log = kmsg.read_log()
    for i, line in enumerate(log):
        if marker.token in line:
            return log[i + 1:]
    return log  # marker never landed (see Marker.write()) -- fall back to all


def kmsg_seconds(line):
    m = KMSG_TS.search(line)
    return float(m.group(1)) if m else None


def wait_for_settle(marker, timeout):
    """Count enumeration attempts until a clean firmware line, after a single
    fixed sleep -- no polling during the window.

    An earlier version of this polled kmsg every 100ms while waiting, each
    poll forking a `dmesg` subprocess. On pi1test's single ARM11 core that
    competes directly with the same CPU the probe's kworker runs on --
    functionally the same masking effect as the dev_info()-to-serial-console
    problem this whole experiment exists to route around, just via process
    scheduling instead of UART blocking. Confirmed the hard way: the
    settle_us=0 baseline, which should reproduce the original ~80% flap
    rate, did not flap at all under the polling version. So: sleep for the
    whole timeout, untouched, then read the log exactly once.

    Returns (attempts, settled, elapsed_s). attempts counts every
    "probe ... failed" line seen before the first OK_MARKER line, so a clean
    cycle is attempts=1 (the one that worked), matching how the manual
    sessions in issue #48 were counted by hand. elapsed_s comes from the
    kernel's own monotonic timestamps in the captured lines, not wall clock,
    per the project's own preference for kmsg-sourced timing.
    """
    time.sleep(timeout)
    seen = lines_since(marker)
    fails = sum(1 for l in seen if FAIL_MARKER in l)
    settled = any(OK_MARKER in l for l in seen)
    ts = [t for t in (kmsg_seconds(l) for l in seen) if t is not None]
    elapsed = (ts[-1] - ts[0]) if len(ts) >= 2 else None
    return (fails + 1 if settled else fails), settled, elapsed


def run_value(ko_path, value, range_us, cycles, off_seconds, timeout, save_trace_dir):
    """value is an int (issue48_settle_us) or the literal string
    "cond_resched"."""
    reload_with(ko_path, value, range_us, trace_enabled=bool(save_trace_dir))
    results = []
    for i in range(cycles):
        marker = kmsg.Marker(f"issue48-sweep-{value}-{i}")
        if save_trace_dir:
            clear_trace()
        marker.write()
        ok, detail = power.cycle(off_seconds)
        if not ok:
            print(f"  cycle {i + 1}/{cycles}: power cycle failed: {detail}",
                  file=sys.stderr)
            results.append({"attempts": None, "settled": False})
            continue
        attempts, settled, elapsed = wait_for_settle(marker, timeout)
        status = "settled" if settled else "TIMED OUT"
        elapsed_str = f"{elapsed:.1f}s" if elapsed is not None else "?"
        print(f"  cycle {i + 1}/{cycles}: {attempts} attempt(s), "
              f"{status}, kernel-log span {elapsed_str}")
        entry = {"attempts": attempts, "settled": settled,
                 "elapsed_s": round(elapsed, 1) if elapsed is not None else None}
        if save_trace_dir:
            # Written after wait_for_settle()'s single sleep, deliberately:
            # reading the trace file has its own cost, and doing that before
            # the sleep completes would be one more thing perturbing the
            # very race this experiment measures.
            trace_dest = os.path.join(save_trace_dir,
                                       f"settle{value}-cycle{i}.trace")
            trace_text = save_trace(trace_dest)
            entry["trace"] = trace_dest
            if trace_text:
                if value == "cond_resched":
                    actual = measure_delays(trace_text, "cond_resched start",
                                             "cond_resched done")
                    label = "cond_resched() actual duration"
                elif value:
                    actual = measure_delays(trace_text, f"settle {value} us start",
                                             "settle done")
                    label = "usleep_range wakeup latency"
                else:
                    actual = []
                if actual:
                    entry["actual_us"] = actual
                    print(f"    requested {value}, measured {actual} ({label})")
        results.append(entry)
    return results


def summarize(value, results):
    label = "issue48_cond_resched=1" if value == "cond_resched" else f"issue48_settle_us={value}"
    ok = [r for r in results if r["attempts"] is not None]
    if not ok:
        return f"{label}: no usable cycles"
    attempts = [r["attempts"] for r in ok]
    flapped = sum(1 for a in attempts if a > 1)
    timed_out = sum(1 for r in ok if not r["settled"])
    return (f"{label}: "
            f"{flapped}/{len(ok)} cycles flapped, "
            f"attempts min={min(attempts)} max={max(attempts)}, "
            f"{timed_out} timed out")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ko_path")
    ap.add_argument("--values", default="0,2000,5000,7500,15000",
                     help="comma-separated issue48_settle_us candidates, "
                          "plus the literal 'cond_resched' to test "
                          "issue48_cond_resched=1 instead")
    ap.add_argument("--range-us", type=int, default=1000,
                     help="issue48_settle_range_us: usleep_range() width "
                          "added on top of each --values entry, e.g. "
                          "--values 50 --range-us 50 tests "
                          "usleep_range(50, 100)")
    ap.add_argument("--cycles", type=int, default=10)
    ap.add_argument("--off-seconds", type=float, default=power.DEFAULT_OFF_SECONDS)
    ap.add_argument("--timeout", type=float, default=30.0,
                     help="max seconds to wait for a cycle to settle")
    ap.add_argument("--out", help="write full per-cycle results as JSON here")
    ap.add_argument("--save-trace",
                     help="directory to save each cycle's ftrace buffer "
                          "(issue48: EP0-transfer trace) into, for cycles "
                          "that still flap even at a given settle value")
    args = ap.parse_args()

    if not os.path.isfile(args.ko_path):
        sys.exit(f"no such file: {args.ko_path}")
    if not power.available():
        sys.exit("no power switch configured/answering -- see lib/power/ "
                  "(this needs real device power cycles, not a USB unplug)")
    if args.save_trace:
        os.makedirs(args.save_trace, exist_ok=True)

    values = [v if v == "cond_resched" else int(v)
              for v in args.values.split(",")]
    all_results = {}
    for v in values:
        label = "issue48_cond_resched=1" if v == "cond_resched" else f"issue48_settle_us={v}"
        print(f"== {label} ==")
        all_results[v] = run_value(args.ko_path, v, args.range_us, args.cycles,
                                    args.off_seconds, args.timeout,
                                    args.save_trace)
        print()

    print("== summary ==")
    for v in values:
        print(summarize(v, all_results[v]))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(all_results, f, indent=2)
        print(f"\nfull results written to {args.out}")


if __name__ == "__main__":
    main()
