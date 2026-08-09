#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L3: switch sample rates in a loop.

The most important case in the catalog, and the one with the least obvious
criterion.

Capture stall after a rate change is EXPECTED on a healthy build. The driver
logs it deliberately, on every occurrence, so that stall frequency can be
tracked. A test that failed on the presence of a warning would fail every run
on a perfectly good driver.

So what is checked is whether each rate change ends with a working stream:

    stalled and recovered  -> pass, and counted
    stalled and stuck      -> fail

Stall counts and reset-completion delays come from the kernel log and are
classified by the runner, not here. This case's job is to provoke the
transitions and confirm audio still flows after each one.

The rate list is deliberately not sorted: the failure is worst on a downward
switch, so a sorted sweep would systematically test the easy direction.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402
from lib import alsa               # noqa: E402

CHANNELS = 4
FORMAT = "S24_3LE"


def play_briefly(device, rate, seconds):
    gen = subprocess.Popen(
        ["sox", "-n", "-r", str(rate), "-c", str(CHANNELS), "-b", "24",
         "-e", "signed-integer", "-t", "raw", "-",
         "synth", str(seconds), "sine", "E3", "gain", "-6"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    p = subprocess.Popen(
        ["aplay", "-D", device, "-r", str(rate), "-c", str(CHANNELS),
         "--format", FORMAT, "-t", "raw"],
        stdin=gen.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True)
    gen.stdout.close()
    try:
        _out, err = p.communicate(timeout=seconds + 30)
    except subprocess.TimeoutExpired:
        p.kill()
        gen.kill()
        return 124, "timed out"
    gen.wait(timeout=5)
    return p.returncode, err


def interleave(rates):
    """Order the sweep so every step is a real change, including downward.

    Alternating from the ends -- highest, lowest, second highest, ... -- makes
    every transition a large one in an alternating direction, which is the
    shape that provokes the fault.
    """
    rs = sorted(rates, reverse=True)
    out, lo, hi = [], 0, len(rs) - 1
    while lo <= hi:
        out.append(rs[lo])
        if lo != hi:
            out.append(rs[hi])
        lo, hi = lo + 1, hi - 1
    return out


def main():
    c = Case()
    c.require_card()
    c.require_tools("aplay", "sox")

    rates = interleave(c.params.get("rates", [44100, 48000, 88200, 96000]))
    seconds = float(c.params.get("seconds_per_rate", 1))
    loops = int(c.params.get("iterations_per_run", 10))
    device = c.device or alsa.device_name(c.card)

    changes = 0
    failures = 0
    xruns_before = alsa.xruns(c.card, "pcm0p")
    t0 = time.time()

    for loop in range(1, loops + 1):
        for rate in rates:
            changes += 1
            rc, err = play_briefly(device, rate, seconds)
            if rc != 0:
                failures += 1
                # This is the real failure mode: the stream did not come back
                # after the change. One is enough to fail the case, but keep
                # going -- whether it is one rate or every rate is a
                # different bug, and the counts say which.
                c.fail(f"loop {loop}, {rate} Hz: playback failed after rate "
                       f"change (exit {rc}) "
                       f"{(err or '').strip().splitlines()[-1][:100] if err else ''}")

    c.metric("rate_changes", changes)
    c.metric("failures", failures)
    c.metric("xruns", max(0, alsa.xruns(c.card, "pcm0p") - xruns_before))
    c.metric("elapsed_s", round(time.time() - t0, 1))
    if changes:
        c.metric("failure_rate_pct", round(100.0 * failures / changes, 2))

    c.note("stall and reset-delay counts come from the kernel log; a stall "
           "that recovered is expected and is not a failure")
    c.done()


if __name__ == "__main__":
    main()
