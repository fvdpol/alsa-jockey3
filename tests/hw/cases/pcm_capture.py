#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L3: capture at each supported rate.

Checks that capture runs and delivers the expected byte count. It deliberately
does NOT fail on silence: with nothing connected to the inputs, silence is the
correct result. Signal presence is JT-AUDIO-004's job, and conflating the two
would make this case fail for a reason that has nothing to do with the driver.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402
from lib import alsa               # noqa: E402

CHANNELS = 6                       # fixed by the driver: min == max
FORMAT = "S24_3LE"
BYTES_PER_SAMPLE = 3


def nonzero_channels(path, channels):
    """Which channels carry anything at all, from a raw S24_3LE capture."""
    frame = channels * BYTES_PER_SAMPLE
    seen = [False] * channels
    try:
        with open(path, "rb") as f:
            data = f.read(frame * 20000)
    except OSError:
        return []
    for off in range(0, len(data) - frame + 1, frame):
        for ch in range(channels):
            s = data[off + ch * BYTES_PER_SAMPLE:
                     off + (ch + 1) * BYTES_PER_SAMPLE]
            if s and any(s):
                seen[ch] = True
        if all(seen):
            break
    return [i for i, v in enumerate(seen) if v]


def main():
    c = Case()
    c.require_card()
    c.require_tools("arecord")

    rates = c.params.get("rates", [44100, 48000, 88200, 96000])
    seconds = int(c.params.get("seconds", 3))
    device = c.device or alsa.device_name(c.card)

    total_xruns = 0
    for rate in rates:
        raw = os.path.join(c.workdir, f"capture_{rate}.raw")
        before = alsa.xruns(c.card, "pcm0c")
        try:
            p = subprocess.run(
                ["arecord", "-D", device, "-d", str(seconds), "-f", FORMAT,
                 "-c", str(CHANNELS), "-r", str(rate), "-t", "raw", raw],
                capture_output=True, text=True, timeout=seconds + 30)
            rc, err = p.returncode, p.stderr
        except subprocess.TimeoutExpired:
            rc, err = 124, "arecord did not finish"
        after = alsa.xruns(c.card, "pcm0c")
        delta = max(0, after - before)
        total_xruns += delta

        if rc != 0:
            c.fail(f"{rate} Hz: arecord exited {rc}: "
                   f"{(err or '').strip()[:120]}")
            continue
        if delta:
            c.fail(f"{rate} Hz: {delta} xrun(s)")

        got = os.path.getsize(raw) if os.path.exists(raw) else 0
        expect = rate * seconds * CHANNELS * BYTES_PER_SAMPLE
        c.metric(f"captured_bytes_{rate}", got)
        # Allow a period's worth of slack at either end -- arecord stops on a
        # period boundary, not on an exact sample count.
        if got < expect * 0.9:
            c.fail(f"{rate} Hz: captured {got} bytes, expected about {expect}")

        live = nonzero_channels(raw, CHANNELS)
        c.metric(f"nonzero_channels_{rate}", live)

    c.metric("xruns", total_xruns)
    c.metric("rates_tested", len(rates))
    c.done()


if __name__ == "__main__":
    main()
