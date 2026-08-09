#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L3: suspend and resume, then confirm the device still works.

From suspend_resume_test_plan.md. The device loses stream sync across suspend,
so the driver deliberately does not advertise SNDRV_PCM_INFO_RESUME -- an
application dying with -ESTRPIPE is the CORRECT outcome, and JT-PM-002 is the
case that checks that. Here the stream is idle, and what is checked is that
the machine comes back and the card is usable without reloading the driver.

This case suspends the machine it is running on. That is worth stating twice:
if the machine does not wake, the run ends here, and the evidence is on the
serial console.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402
from lib import alsa               # noqa: E402


def main():
    c = Case()
    if os.geteuid() != 0:
        c.blocked("needs root to suspend")
    c.require_card()
    c.require_tools("rtcwake")

    sleep_s = int(c.params.get("sleep_seconds", 10))

    before = alsa.substreams(c.card)
    t0 = time.time()
    try:
        p = subprocess.run(["rtcwake", "-m", "mem", "-s", str(sleep_s)],
                           capture_output=True, text=True,
                           timeout=sleep_s + 180)
        rc, err = p.returncode, p.stderr
    except subprocess.TimeoutExpired:
        c.fail(f"machine did not resume within {sleep_s + 180}s")
        c.done()
    elapsed = time.time() - t0

    if rc != 0:
        c.fail(f"rtcwake exited {rc}: {(err or '').strip()[:160]}")
        c.done()

    c.metric("cycle_s", round(elapsed, 1))
    # rtcwake returns as soon as the kernel is back; give USB re-enumeration
    # a moment before deciding the card is gone.
    deadline = time.time() + 15
    idx = None
    while time.time() < deadline:
        idx, _ = alsa.find_card()
        if idx is not None:
            break
        time.sleep(0.5)

    if idx is None:
        c.fail("card did not come back after resume")
        c.done()

    c.metric("resume_ms", round((time.time() - t0 - sleep_s) * 1000, 1))

    after = alsa.substreams(idx)
    for kind in ("playback", "capture", "rawmidi"):
        if len(after[kind]) != len(before[kind]):
            c.fail(f"{kind} substreams changed across suspend: "
                   f"{len(before[kind])} -> {len(after[kind])}")

    c.done()


if __name__ == "__main__":
    main()
