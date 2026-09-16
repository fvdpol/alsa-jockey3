#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L3: suspend the machine while the watchdog is recovering a stall.

The predicate for issue #43. jockey3_suspend() holds rate_mutex across
jockey3_stop_urbs(), which disarms the watchdog with the non-sync cancel and
zeroes the liveness timestamps. A tick that is already running parks on the
mutex, wakes when suspend drops it, reads the zeroed timestamps as a stall, and
-- before the fix -- restarts the URB ring on a device the PM core believes is
suspended, then escalates to resetting it.

HOW THE RACE IS PROVOKED
------------------------
It cannot be provoked by ordinary means, which the first version of this case
demonstrated: recovery_entered was 0 across six suspends. jockey3_stop_urbs()
sets urb_stream->stopping before it zeroes the liveness timestamps, and
jockey3_watchdog_check() returns early on that flag, so between
jockey3_suspend() and the resume path's jockey3_start_urbs() the watchdog cannot
report a stall at all. Cutting USB power does not help either -- that
disconnects the device, and jockey3_recover_urb_stream() returns -ENODEV at its
first check.

So the stall is injected. The driver on branch
test/issue43-suspend-stall-injection reuses the mechanism from
dev/jockey3-watchdog-stall-injection -- every completion for a direction is
dropped for a window, producing genuine silence rather than a lie about
liveness -- and makes its two timing constants module parameters so a case can
line the window up with something else. Here that is a suspend.

The injection runs in periodic mode, and that is load-bearing. One-shot gives a
single opportunity that is over in well under a second: the case measured
recovery entering once per suspend that way, always finishing before the suspend
landed, and so passed against a driver with the bug still in it -- the negative
control caught that. Repeating keeps recovery cycling for as long as the case
needs, with the window kept shorter than the period so each cycle's escalated
reset lands after the drops stop and recovery succeeds. Sustained churn, without
driving the ladder to give up.

priv.stall_inject() fails rather than succeeding quietly against a driver
without those parameters, so this case cannot mistake "knob absent" for "stall
provoked".

The race needs the tick inside jockey3_recover_urb_stream(), past detection and
blocked on rate_mutex, when jockey3_suspend() takes it -- so the suspend is
issued once the window has opened and the watchdog has had a tick to notice.

The discriminators are the two dev_warn lines only jockey3_start_urbs() can
produce: "URB stream restarted after stalling", emitted by
jockey3_watchdog_clear_stall() from inside it, and the reset escalation behind
it. The dev_dbg pair that would show the restart directly is not relied on,
because enabling dynamic debug on this host writes to a serial console and
perturbs the very timing being measured.

A run in which the watchdog never entered recovery is reported blocked, not
passed. JT-PM-001 passes on a driver with this bug; a green here must mean the
race was reached and survived, never that it was missed.

Like JT-PM-001, this suspends the machine it runs on.
"""

import os
import random
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case            # noqa: E402
from lib import alsa, kmsg, priv     # noqa: E402

# The driver's own PM callbacks. These are the only exact bracket for "after
# jockey3_suspend() ran": the PM core's "suspend entry" is logged before
# userspace is even frozen, with the device still fully live, so recovery
# between it and the driver callback is legitimate and must not be counted.
RE_SUSPENDED = re.compile(r"USB suspend, stopping URBs")
RE_RESUMED = re.compile(r"USB (?:reset )?resume, restoring device")

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

    ok, why = priv.dyndbg_pm(True)
    if not ok:
        c.blocked("cannot enable the driver's suspend/resume messages, which "
                  f"are the only exact bracket for this measurement ({why})")
    # Keep them off the serial console: writing there synchronously perturbs the
    # very timing being measured (docs/test_strategy.md).
    priv.printk_console(1)

    # Widen the warm grace: it IS the window this case has to hit. A tick sits
    # in it between restarting the ring and escalating to a reset, and the
    # suspend has to land there. At the 150 ms default that is a few per cent of
    # each injection period; at 1000 ms against a 2000 ms period it is about
    # half, which is what makes the case converge in a handful of suspends
    # rather than tens.
    grace_ms = int(c.params.get("warm_grace_ms", 1000))
    ok, why = priv.grace_ms(int(c.params.get("cold_grace_ms", 200)), grace_ms)
    if not ok:
        c.blocked(f"cannot set the start graces ({why})")

    after_s = int(c.params.get("stall_after_s", 2))
    window_ms = int(c.params.get("stall_window_ms", 200))
    period_ms = int(c.params.get("stall_period_ms", 2000))
    ok, why = priv.stall_inject(after_s, window_ms, period_ms)
    if not ok:
        c.blocked("no debug_stall_inject_* parameters, so no stall can be "
                  "provoked: this needs the driver built from "
                  f"test/issue43-suspend-stall-injection ({why})")

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

        # Jitter across one injection period. A fixed offset samples the same
        # phase every time, which is how an earlier version of this case missed
        # the bug on every suspend: the vulnerable phase was always just outside
        # where it looked.
        time.sleep(max(0.0, after_s - settle_s) +
                   random.uniform(0.0, period_ms / 1000.0))

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
        # Narrow to what the driver itself considered suspended. Everything
        # before jockey3_suspend() is a live device recovering legitimately --
        # which is exactly what the injection provoked, so counting it would
        # fail every build, fixed or not.
        suspended = []
        inside = False
        for line in window:
            if RE_SUSPENDED.search(line):
                inside = True
                continue
            if RE_RESUMED.search(line):
                inside = False
                continue
            if inside:
                suspended.append(line)

        n_entered = sum(1 for line in window if RE_ENTERED.search(line))
        n_restarted = sum(1 for line in suspended if RE_RESTARTED.search(line))
        n_reset = sum(1 for line in suspended if RE_RESET.search(line))
        if not any(RE_SUSPENDED.search(line) for line in window):
            c.fail(f"iteration {i}: no suspend callback in the log, so nothing "
                   "could be bracketed")
        entered += n_entered
        restarted += n_restarted
        reset += n_reset

        if n_restarted or n_reset:
            c.fail(f"iteration {i}: the ring was rebuilt while the device was "
                   f"suspended ({n_restarted} restart(s), {n_reset} reset(s))")

        c.progress(f"cycle {i}/{iterations}  recovery entered {n_entered}x, "
                   f"restarts {n_restarted}, resets {n_reset}, "
                   f"resume {round(time.time() - t0 - sleep_s, 1)}s")

    priv.dyndbg_pm(False)
    priv.stall_inject(after_s, 0, 0)
    priv.grace_ms(200, 150)

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
