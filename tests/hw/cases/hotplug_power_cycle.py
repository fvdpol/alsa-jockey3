#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L3: JT-HOTPLUG-003 -- power cycle at the mains during active playback.

Distinct from JT-HOTPLUG-001 (cable pull) and JT-PROBE-003 (VBUS at the hub):
the Jockey 3 is self-powered, and cutting its own mains supply without
touching the USB cable is the only way to reach a specific failure mode
where in-flight URBs error out repeatedly before usbcore ever calls
disconnect(). A hub port power-off is a clean, instant VBUS drop -- measured
2026-08-10, it produces -19 (ENODEV) at once, and the "N consecutive URB
errors" accounting this case exists to exercise is never reached. Only the
device's own mains switch produces the -71 (EPROTO) burst.

WHAT ACTUALLY HAPPENS, MEASURED ON HARDWARE (2026-08-19, pi4test, idle)
--------------------------------------------------------------------------
Cutting the relay produces, within about 10 ms and with or without an open
PCM substream (URBs run free for the device's lifetime regardless -- see
jockey3.c's own DOC: block):

    Playback URB error: -71 (1 consecutive)
    ...
    Playback stopped after 8 consecutive URB errors; deferring recovery
    Capture stopped after 8 consecutive URB errors; deferring recovery
    MIDI IN stopped after 8 consecutive URB errors
    usb 1-1.1.1: USB disconnect, device number NN          [~180 ms later]

That is jockey3_urb_error_give_up() in jockey3.c, called once per direction
per URB completion once JOCKEY3_MAX_URB_ERRORS is crossed. The card then
disappears from /proc/asound in well under a second, and reappears within a
few seconds of power being restored (measured: ~1.2s down, ~2.3s back) -- so
/proc/asound presence is a fast, reliable witness for both halves of the
cycle, same as JT-AUDIO-005's reenumerate() already assumes.

WHAT THIS CASE ADDS OVER THE IDLE MEASUREMENT ABOVE
-------------------------------------------------------
The give-up message fires from URBs that run regardless of whether a PCM
substream is open, so an idle power cycle already reaches it -- that is
JT-AUDIO-005's territory (an idle device that stops producing audio). What
only an OPEN, ACTIVE playback stream can show is the userspace-visible half:
does aplay's write() come back with an error so the application exits,
or does it block forever on a device that has quietly stopped answering?
That is what the manual steps (now the operator fallback below) actually
checked, and it is the reason this case starts a real aplay process rather
than just cutting power and reading dmesg.

NOT "hardware may need power-cycling"
------------------------------------------
The catalog's manual steps used to quote that string as the give-up message
to look for. It is real (jockey3.c, jockey3_recover_urb_stream()) but
belongs to a recovery escalation, reachable only after BOTH a URB restart
and a full USB reset fail to bring a direction back.
A playback-only power cycle like this one cannot reach it, and forcing a
capture open afterward just to chase a message that only fires when
recovery has already failed twice was rejected: it would turn a
best-case-should-not-happen path into something this case demands on every
run. expect_dmesg in catalog.yaml matches the message actually reached
instead: "stopped after N consecutive URB errors; deferring recovery".

TWO WAYS TO CUT THE POWER, ONE WITNESS FOR BOTH
----------------------------------------------------
Same DevicePower/OperatorPower split as JT-AUDIO-005 (imported from
cases.audio_engine_start rather than duplicated): a relay makes this
unattended, and without one an operator is instructed rather than blocked
on -- instruct() only prints, the bus is what the case actually waits on,
which is what keeps this runnable with `mode: automated` and no `human` in
`requires` even though a person may be the one at the switch. See
JT-AUDIO-005's own docstring for why that distinction matters to unattended
runs.

WHY MODULE UNLOAD/RELOAD RUNS ONCE, AFTER THE LOOP
-------------------------------------------------------
"The module still unloads cleanly afterwards" is in the pass criteria, and
it is the check most likely to catch a real bug: URBs erroring out without
disconnect() ever running (because the device came back before usbcore
noticed, or the error path left some reference uncleaned) is exactly the
shape of bug that leaves a module stuck. It runs once at the end, not once
per cycle, because unloading changes what the next cycle would start from --
per-cycle would make the second cycle onward a different, undocumented test.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case                              # noqa: E402
from lib import alsa, kmsg, power, priv                 # noqa: E402
from cases.audio_engine_start import (                  # noqa: E402
    wait_for_card, wait_for_substreams, DevicePower, OperatorPower,
)
from cases.pcm import PLAYBACK_CHANNELS, FORMAT          # noqa: E402

GIVEUP_RE = "consecutive URB errors"


def start_playback(device, rate):
    """A real, indefinite playback stream -- /dev/urandom into aplay, same
    as the manual steps this case replaces. No fixed duration: it is meant
    to still be running when power is cut, and to be killed or to error out
    on its own once the device goes away.
    """
    return subprocess.Popen(
        ["aplay", "-D", device, "-f", FORMAT, "-c", str(PLAYBACK_CHANNELS),
         "-r", str(rate), "-t", "raw", "/dev/urandom"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True)


def run_cycle(c, i, act, device, rate, off_seconds, settle,
              warmup_seconds, aplay_timeout):
    """One power cycle during active playback. Returns True on success;
    records the failure itself via c.fail() before returning False, per the
    runner's "last stderr line is the reason" convention -- nothing may be
    printed after a fail.
    """
    p = start_playback(device, rate)
    time.sleep(warmup_seconds)
    if p.poll() is not None:
        c.fail(f"cycle {i}: aplay exited (rc={p.returncode}) before power "
               f"was even cut")
        return False

    mark = kmsg.Marker(f"{c.id}#cycle{i}")
    mark.write()

    t_off = time.time()
    ok, detail = act.off()
    if not ok:
        p.kill()
        p.wait(timeout=5)
        c.fail(f"cycle {i}: could not take the device down ({act.kind}): "
               f"{detail}")
        return False

    # Both halves of the disconnect should land close together -- the card
    # leaving /proc/asound and aplay noticing its writes are failing -- so
    # they are awaited in one loop rather than sequentially, and whichever
    # is still outstanding at the deadline names the failure.
    deadline = time.time() + settle
    card_gone = aplay_done = None
    while time.time() < deadline and (card_gone is None or aplay_done is None):
        if card_gone is None and alsa.find_card()[0] is None:
            card_gone = time.time()
        if aplay_done is None and p.poll() is not None:
            aplay_done = time.time()
        if card_gone is None or aplay_done is None:
            time.sleep(0.05)

    if aplay_done is None:
        p.kill()
        p.wait(timeout=5)
        c.fail(f"cycle {i}: aplay did not exit within {settle:g}s of power "
               f"loss -- hung rather than erroring")
        return False
    if card_gone is None:
        p.wait(timeout=5)
        c.fail(f"cycle {i}: card still present {settle:g}s after power was "
               f"cut ({act.kind})"
               + (f" -- {act.gone_hint}" if act.gone_hint else ""))
        return False
    if p.returncode == 0:
        c.fail(f"cycle {i}: aplay exited 0 after power loss -- expected an "
               f"error, not a clean exit")
        return False

    c.metric(f"aplay_error_ms_{i}", round((aplay_done - t_off) * 1000, 1))
    c.metric(f"card_gone_ms_{i}", round((card_gone - t_off) * 1000, 1))

    gave_up = any(GIVEUP_RE in line
                 for line in kmsg.slice_since(kmsg.read_log(), mark))
    c.metric(f"gave_up_{i}", gave_up)
    if not gave_up:
        c.note(f"cycle {i}: no \"{GIVEUP_RE}\" line seen -- the device may "
               f"have disconnected before URBs had a chance to accumulate "
               f"errors, which is not itself a defect but means this cycle "
               f"did not exercise the path this case is for")

    time.sleep(off_seconds)

    ok, detail = act.on()
    if not ok:
        c.fail(f"cycle {i}: could not restore power ({act.kind}): {detail}")
        return False

    idx, seen = wait_for_card(True, settle)
    if seen is None:
        c.fail(f"cycle {i}: card did not come back within {settle:g}s "
               f"({act.kind})" + (f" -- {act.back_hint}" if act.back_hint else ""))
        return False
    c.metric(f"reenumerate_ms_{i}", round((seen - t_off) * 1000, 1))

    settle_ms, missing = wait_for_substreams(idx, settle)
    if missing:
        c.fail(f"cycle {i}: no {', '.join(missing)} after re-enumeration")
        return False
    c.metric(f"resubstream_ms_{i}", settle_ms)
    return True


def main():
    c = Case()
    c.require_card()
    c.require_tools("aplay")

    ok, why = priv.available()
    if not ok:
        c.blocked(f"privileged helper unavailable: {why}")

    iterations = int(c.params.get("iterations_per_run", 1))
    off_seconds = float(c.params.get("off_seconds", 5))
    settle = float(c.params.get("settle_seconds", 8))
    warmup_seconds = float(c.params.get("warmup_seconds", 1))
    aplay_timeout = settle
    rate = int(c.params.get("rate", 44100))
    operator_timeout = float(c.params.get("operator_timeout_seconds", 120))

    if power.available():
        act = DevicePower(settle)
    elif c.attended:
        act = OperatorPower(c, operator_timeout)
    else:
        c.blocked("needs either a configured mains switch (see lib/power/) "
                  "or an operator at the switch")
    c.metric("power_control", act.kind)

    device = alsa.device_name(c.card)
    ok_cycles = 0
    for i in range(1, iterations + 1):
        c.status(f"  cycle {i}/{iterations}  power cycling during playback")
        if run_cycle(c, i, act, device, rate, off_seconds, settle,
                    warmup_seconds, aplay_timeout):
            ok_cycles += 1
            c.progress(f"  cycle {i}/{iterations}  OK")
        else:
            break

    c.metric("cycles", ok_cycles)
    if ok_cycles < iterations:
        c.done()
        return

    c.status("  checking the module still unbinds and rebinds cleanly")
    rc, _out, err = priv.unload_module()
    c.metric("unload_ok", rc == 0)
    if rc != 0:
        c.fail(f"module would not unload after the power cycle: "
               f"{(err or '').strip()[:160]}")
        c.done()
        return
    rc, _out, err = priv.load_module()
    c.metric("reload_ok", rc == 0)
    if rc != 0:
        c.fail(f"module would not reload after the power cycle: "
               f"{(err or '').strip()[:160]}")
        c.done()
        return
    idx, seen = wait_for_card(True, settle)
    if seen is None:
        c.fail(f"card did not reappear within {settle:g}s of reloading "
               f"the module")
    c.done()


if __name__ == "__main__":
    main()
