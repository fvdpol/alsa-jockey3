#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L3: suspend the machine while the watchdog is recovering a stall.

The predicate for issue #43. jockey3_suspend() holds rate_mutex across
jockey3_stop_urbs(), which disarms the watchdog with the non-sync cancel and
zeroes the liveness timestamps. A tick that is already running parks on the
mutex, wakes when suspend drops it, reads the zeroed timestamps as a stall, and
-- before the fix -- restarts the URB ring on a device the PM core believes is
suspended, then escalates to resetting it.

THIS CASE DOES NOT YET REACH THE RACE. READ THIS BEFORE TRUSTING A PASS.
-----------------------------------------------------------------------
The case was written on the assumption that suspend manufactures the appearance
of a stall by zeroing the timestamps, so a tick landing just after it would
enter recovery. That is wrong, and the first run on hardware showed it:
recovery_entered was 0 across six suspends.

jockey3_stop_urbs() sets urb_stream->stopping before it zeroes the timestamps,
and jockey3_watchdog_check() returns early on exactly that flag. From
jockey3_suspend() until the resume path calls jockey3_start_urbs(), the watchdog
cannot report a stall at all.

So the race needs what the Sashiko report actually described: a tick already
INSIDE jockey3_recover_urb_stream(), past stall detection and blocked on
rate_mutex, at the moment suspend takes it. That requires a genuine mid-stream
stall in progress when the machine suspends, and nothing in the test framework
can produce one on demand -- cutting USB power disconnects the device instead,
which makes jockey3_recover_urb_stream() return -ENODEV at its first check.

Reaching it needs fault injection: a development-time knob that forces a
direction to be seen as stalled. That is a driver change and a decision about
the no-test-hooks rule, so it is not made here.

What the case is worth meanwhile: a no-regression check that suspending with a
stream open leaves no restart or reset behind, and an honest report that the
race was not provoked. It reports blocked, never pass, when recovery was not
entered -- the trap JT-PM-001 falls into for this bug is passing without ever
reaching it.

The discriminators, once the race can be provoked, are the two dev_warn lines
only jockey3_start_urbs() can produce: "URB stream restarted after stalling",
emitted by jockey3_watchdog_clear_stall() from inside it, and the reset
escalation behind it. The dev_dbg pair that would show the restart directly is
not relied on, because enabling dynamic debug on this host writes to a serial
console and perturbs the very timing being measured.

Like JT-PM-001, this suspends the machine it runs on.
"""

import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case            # noqa: E402
from lib import alsa, kmsg, priv     # noqa: E402

# Entered the ladder: logged before rate_mutex is taken, so it appears whether
# or not the fix is in. Counting it is how the case knows it provoked anything.
RE_ENTERED = re.compile(
    r"(?:Playback|Capture) stream stalled \(watchdog\); restarting URBs to recover")

# Only reachable from inside jockey3_start_urbs(), so these mean the ring really
# was rebuilt while the device was suspended.
RE_RESTARTED = re.compile(
    r"(?:Playback|Capture) URB stream restarted after stalling")
RE_RESET = re.compile(
    r"(?:Playback|Capture) stream still stalled after URB restart; queuing full USB reset")


def playback(card, rate):
    """A playback stream, held open for the duration."""
    return subprocess.Popen(
        ["aplay", "-D", f"hw:{card},0", "-f", "S24_3LE", "-c", "4",
         "-r", str(rate), "/dev/urandom"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    c = Case()
    ok, why = priv.available()
    if not ok:
        c.blocked(f"cannot suspend: {why}")
    c.require_card()
    c.require_tools("aplay")

    iterations = int(c.params.get("iterations_per_run", 6))
    sleep_s = int(c.params.get("sleep_seconds", 8))
    settle_s = float(c.params.get("settle_seconds", 3))
    rate = int(c.params.get("rate", 48000))

    entered = restarted = reset = 0
    card = c.card

    for i in range(1, iterations + 1):
        c.status(f"cycle {i}/{iterations}  suspending with playback open")

        proc = playback(card, rate)
        time.sleep(settle_s)
        if proc.poll() is not None:
            proc.wait()
            c.fail(f"iteration {i}: playback exited before the suspend")
            break

        mark = kmsg.Marker(f"{c.id}#cycle{i}")
        mark.write()

        t0 = time.time()
        rc, _out, err = priv.rtcwake_mem(sleep_s)
        if rc == 124:
            c.fail(f"iteration {i}: machine did not resume")
            break
        if rc != 0:
            c.fail(f"iteration {i}: rtcwake exited {rc}: {(err or '').strip()[:160]}")
            break

        # The suspend killed the stream; the driver does not advertise
        # SNDRV_PCM_INFO_RESUME, so aplay dying here is correct (see JT-PM-002).
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

        deadline = time.time() + 20
        idx = None
        while time.time() < deadline:
            idx, _ = alsa.find_card()
            if idx is not None:
                break
            time.sleep(0.5)
        if idx is None:
            c.fail(f"iteration {i}: card did not come back after resume")
            break
        card = idx

        window = kmsg.slice_since(kmsg.read_log(), mark)
        n_entered = sum(1 for line in window if RE_ENTERED.search(line))
        n_restarted = sum(1 for line in window if RE_RESTARTED.search(line))
        n_reset = sum(1 for line in window if RE_RESET.search(line))
        entered += n_entered
        restarted += n_restarted
        reset += n_reset

        if n_restarted or n_reset:
            c.fail(f"iteration {i}: the ring was rebuilt while the device was "
                   f"suspended ({n_restarted} restart(s), {n_reset} reset(s))")

        c.progress(f"cycle {i}/{iterations}  recovery entered {n_entered}x, "
                   f"restarts {n_restarted}, resets {n_reset}, "
                   f"resume {round(time.time() - t0 - sleep_s, 1)}s")

    c.metric("cycles", iterations)
    c.metric("recovery_entered", entered)
    c.metric("restarts_while_suspended", restarted)
    c.metric("resets_while_suspended", reset)

    if not c.failed and entered == 0:
        c.blocked(
            f"the watchdog never entered recovery across {iterations} suspends, "
            "so this run did not reach the race and proves nothing about it")

    c.done()


if __name__ == "__main__":
    main()
