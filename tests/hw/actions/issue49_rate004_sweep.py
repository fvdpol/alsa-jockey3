#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""issue49 -- sweep issue48_settle_us/issue48_settle_range_us directly
against the real JT-RATE-004 case, instead of a synthetic reproduction.

NEVER MERGE, NEVER RUN except on the test/issue49-mid-transaction-power-loss
branch. See github.com/fvdpol/alsa-jockey3/issues/49 (xref #48).

WHY THE REAL CASE INSTEAD OF issue49_vbus_cut_sweep.py
-------------------------------------------------------
That script isolated the condition (a VBUS-only cut landing while rate
changes are actively racing) with a synthetic two-arm comparison. JT-RATE-004
already reproduces the same failure reliably in its own right -- every run
so far has hit it -- so there is no need for a separate synthetic harness to
find out *whether* a given settle delay is enough: just reload the
instrumented module at that delay and run the real case, small N, and read
its own dmesg the same way the historical failing runs were read (this
script does not touch JT-RATE-004 itself, or its params, beyond
iterations_per_run and the timeout that scales with it).

    issue49_rate004_sweep.py <path-to-ko> [--values 25,100,250,500,1000,2000]
        [--range-us 10] [--iterations 20] [--timeout 900]
        [--no-power-cycle-first]

Leaves the module reloaded at the first --values entry (the shipped delay,
by default) when done, so the rig is not left mid-sweep.

Each value's JT-RATE-004 run starts with a real mains power cycle by
default (lib.power.cycle(), a cold boot -- see issue49_vbus_cut_sweep.py's
module docstring for why that's not the same as JT-RATE-004's own
priv.usb_power() cuts). #50 found that several consecutive JT-RATE-004
runs back to back, with no recovery pause, can wedge the device -- power
cycling before each value's run keeps every run starting from the same
known-clean firmware state instead of silently inheriting whatever state
the previous value's run left behind. --no-power-cycle-first skips this.
"""

import argparse
import glob
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib import alsa, power  # noqa: E402

MODULE = "snd_reloop_jockey3"
HERE = os.path.dirname(os.path.abspath(__file__))
TESTS_HW = os.path.normpath(os.path.join(HERE, ".."))


def wait_for_card(timeout):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        idx, _ = alsa.find_card()
        if idx is not None and alsa.control_is_live(idx):
            return idx
        time.sleep(0.1)
    return None


def reload_with(ko_path, settle_us, range_us):
    subprocess.run(["sudo", "rmmod", MODULE], check=False, capture_output=True)
    r = subprocess.run(["sudo", "insmod", ko_path,
                         f"issue48_settle_us={settle_us}",
                         f"issue48_settle_range_us={range_us}"])
    if r.returncode != 0:
        sys.exit(f"insmod failed for settle_us={settle_us}")


def run_jt_rate_004(iterations, timeout, note):
    p = subprocess.run(
        ["python3", "runner.py", "--case", "JT-RATE-004", "--unattended",
         "--param", f"iterations_per_run={iterations}",
         "--timeout", str(timeout), "--note", note],
        cwd=TESTS_HW, capture_output=True, text=True)
    print(p.stdout)
    if p.stderr:
        print(p.stderr, file=sys.stderr)
    m = re.search(r"results -> (\S+)", p.stdout)
    return m.group(1) if m else None


def summarize(run_dir):
    dmesg_path = os.path.join(run_dir, "dmesg.txt")
    if not os.path.isfile(dmesg_path):
        return None
    text = open(dmesg_path, encoding="utf-8", errors="replace").read()
    recoveries = text.count("attempting recovery")
    fail_lines = text.count("probe with driver snd-reloop-jockey3 failed")
    return recoveries, fail_lines


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ko_path")
    ap.add_argument("--values", default="25,100,250,500,1000,2000")
    ap.add_argument("--range-us", type=int, default=10)
    ap.add_argument("--iterations", type=int, default=20)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--no-power-cycle-first", action="store_true",
                     help="skip the mains power cycle before each value's "
                          "run (default: on)")
    args = ap.parse_args()

    if not os.path.isfile(args.ko_path):
        sys.exit(f"no such file: {args.ko_path}")

    values = [int(v) for v in args.values.split(",")]
    summary_lines = []
    try:
        for settle_us in values:
            if not args.no_power_cycle_first:
                print(f"== power-cycling before settle_us={settle_us} ==")
                ok, detail = power.cycle()
                if not ok:
                    sys.exit(f"power cycle failed before settle_us={settle_us}: {detail}")
                if wait_for_card(30.0) is None:
                    sys.exit(f"device did not reappear within 30s "
                             f"before settle_us={settle_us}")
            reload_with(args.ko_path, settle_us, args.range_us)
            note = (f"issue49 JT-RATE-004 sweep settle_us={settle_us} "
                     f"range_us={args.range_us}")
            run_dir = run_jt_rate_004(args.iterations, args.timeout, note)
            if run_dir is None:
                line = f"settle_us={settle_us}: could not find results dir"
                print(line, file=sys.stderr)
                summary_lines.append(line)
                continue
            s = summarize(run_dir)
            if s is None:
                line = f"settle_us={settle_us}: no dmesg.txt in {run_dir}"
                print(line, file=sys.stderr)
                summary_lines.append(line)
                continue
            recoveries, fails = s
            line = (f"settle_us={settle_us} range_us={args.range_us} "
                     f"iterations={args.iterations}: recoveries={recoveries} "
                     f"failed_probe_lines={fails} (~{fails // 2} events) "
                     f"run={run_dir}")
            print(f"== {line} ==\n")
            summary_lines.append(line)
    finally:
        reload_with(args.ko_path, values[0], args.range_us)

    print("\n== sweep summary ==")
    for line in summary_lines:
        print(line)


if __name__ == "__main__":
    main()
