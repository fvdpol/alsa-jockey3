#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L3 semi-automated: MIDI IN survives on-demand streaming's idle stop.

THE GATE, per re/on-demand_streaming.md. On-demand streaming (Milestone 17)
keeps the PCM URB rings stopped while nothing is using the device, restarting
them on a PCM open or an outgoing MIDI byte. Before any of that is built out,
one question decides whether the feature is worth building at all: **does the
device keep delivering MIDI IN on EP 0x83 while the PCM URBs are stopped?**

A controller whose jog wheels, faders and buttons go dead whenever audio is
closed is a functional regression, not a tradeoff -- and this is a DJ
controller whose primary job is MIDI. If this case finds no bytes, the
feature is closed regardless of anything else in the design.

THE HUMAN IS THE ACTUATOR; THE MACHINE IS THE JUDGE. Same division as
JT-MIDI-001/JT-MIDI-004's own MIDI IN cases -- moving a jog wheel is outside
what this suite can automate, but counting bytes on the rawmidi node is not.

WHY THIS UNBINDS AND REBINDS BEFORE IT DOES ANYTHING ELSE
-----------------------------------------------------------
idle_timeout defaults to 600s and jockey3_idle_work() is armed from probe.
The normal state of a rig before someone runs `--case JT-MIDI-008` is that
the driver has been loaded for well over ten minutes with nothing using it --
which means the PCM rings are *already* stopped, and stopped silently: the
dev_dbg this case depends on was off at the time, since nothing had asked for
it yet. Starting from that state, this case would write its marker, wait its
budget, see no fresh deactivation message, and fail a gate that may well have
passed hours ago with no evidence either way.

So the case forces a known state first: unbind, rebind (cases/probe_bind_
unbind.py is the precedent for this cycle), which re-probes the device --
starting the PCM rings and stamping last_activity fresh -- before the short
idle_timeout is set and the wait begins. It does the same at the end,
regardless of outcome: Phase 1 has no restart path (see the DOC: comment at
the top of jockey3.c), so a PASS here would otherwise leave the PCM rings
stopped for every later case run against this target.

HOW THE STOP IS CONFIRMED, NOT ASSUMED
---------------------------------------
Waiting idle_timeout seconds and hoping is not evidence either. This case
sets a short idle_timeout via priv.set_idle_timeout() (tests/hw/priv/
jockey3-testctl, re/on-demand_streaming.md's runtime knob, not a modprobe
parameter -- an unknown module parameter is itself classified as a driver
failure), enables jockey3_idle_work()'s own dev_dbg via priv.dyndbg_ondemand(),
and then reads that line back out of the kernel log before asking the
operator to touch anything. The driver's own word that it stopped, not an
assumption from the clock.

WHAT THIS DOES NOT TEST
------------------------
Not the restart path -- there is none yet on this branch (Phase 1 only). Not
the MIDI OUT trigger. Not whether an outgoing byte survives an idle gap (that
is JT-MIDI-009, planned for Phase 2, once there is something to restart).
This case answers exactly one question and nothing else.
"""

import os
import select
import sys
import time

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402
from lib import alsa, kmsg, priv   # noqa: E402

IDLE_TIMEOUT_S = 5

# Slack over IDLE_TIMEOUT_S before giving up on seeing the driver's own
# deactivation message. JOCKEY3_IDLE_RECHECK_MS is 1000, so 1s of jitter is
# expected; this leaves a wide margin for a loaded scheduler.
WAIT_MARGIN_S = 15

WATCH_SECONDS = 30
POLL_TIMEOUT_S = 0.5

# How long to wait for the card to appear or disappear around a rebind.
REBIND_SETTLE_S = 5


def wait_for_card(present, timeout):
    """Wait for the card to appear or disappear. Returns (index, when|None).

    Copied from cases/probe_bind_unbind.py rather than shared: a rebind can
    land the card at a different index than it started at (dev_idx reuses
    whatever slot is free), so every caller has to re-resolve it this way
    rather than trusting the index it started with.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        idx, _ = alsa.find_card()
        if (idx is not None) == present:
            return idx, time.time()
        time.sleep(0.02)
    idx, _ = alsa.find_card()
    return idx, None


def rebind(settle):
    """Unbind then bind, returning the card's (possibly new) index, or None."""
    rc, _out, err = priv.unbind()
    if rc != 0:
        return None, f"unbind failed: {(err or '').strip()[:120]}"
    _idx, gone = wait_for_card(False, settle)
    if gone is None:
        return None, f"card still present {settle}s after unbind"

    rc, _out, err = priv.bind()
    if rc != 0:
        return None, f"bind failed: {(err or '').strip()[:120]}"
    idx, seen = wait_for_card(True, settle)
    if seen is None:
        return None, f"card did not reappear {settle}s after bind"

    subs = alsa.substreams(idx)
    for kind in ("playback", "capture", "rawmidi"):
        if not subs[kind]:
            return None, f"no {kind} substream after rebind"

    return idx, None


def wait_for_deactivation(c, marker, budget_s):
    """Poll dmesg for jockey3_idle_work()'s own stop message.

    Returns True once seen, False if budget_s runs out first. Polls rather
    than blocking on a single sleep-then-read so the case can report how long
    the actual wait was, not just whether it eventually happened.
    """
    deadline = time.monotonic() + budget_s
    while True:
        remaining = deadline - time.monotonic()
        log = kmsg.read_log()
        sliced = kmsg.slice_since(log, marker)
        if any("On-demand:" in line and "stopping PCM streaming" in line
              for line in sliced):
            return True
        if remaining <= 0:
            return False
        c.status(f"   waiting for the driver to stop the PCM rings "
                 f"({max(0, int(remaining))}s left) ...")
        time.sleep(min(1.0, remaining))


def watch_midi_in(c, node, seconds):
    """Count bytes read from the rawmidi node over a fixed window.

    Same read loop as JT-MIDI-005 (cases/midi_padding.py): the raw character
    device rather than a sequencer client, because the question is whether
    bytes reach the rawmidi stream at all, not whether they parse.
    """
    try:
        fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        c.fail(f"could not open {node}: {e}")
        return 0

    total = 0
    deadline = time.monotonic() + seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([fd], [], [], min(remaining, POLL_TIMEOUT_S))
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except (BlockingIOError, InterruptedError):
                    chunk = b""
                except OSError as e:
                    c.fail(f"read from {node} failed: {e}")
                    break
                total += len(chunk)
            c.status(f"   watching MIDI IN  {max(0, int(deadline - time.monotonic()))}s "
                     f"left  ....  {total} byte(s) seen")
    finally:
        os.close(fd)
    return total


def main():
    c = Case()
    c.require_card()
    if not c.attended:
        c.blocked("nobody is at the keyboard; this case needs an operator "
                  "generating MIDI IN traffic")

    ok, why = priv.available()
    if not ok:
        c.blocked(f"cannot control idle_timeout, dyndbg or rebind the driver: {why}")

    idle_timeout_s = int(c.params.get("idle_timeout_s", IDLE_TIMEOUT_S))
    wait_margin_s = int(c.params.get("wait_margin_s", WAIT_MARGIN_S))
    watch_seconds = int(c.params.get("watch_seconds", WATCH_SECONDS))
    settle = float(c.params.get("rebind_settle_s", REBIND_SETTLE_S))

    # Precondition, checked BEFORE anything below touches the device: nothing
    # else has the PCM open. Rebinding out from under an active session would
    # be rude at best, and the ring would never look idle to us anyway.
    if alsa.pcm_status(c.card, "pcm0p") or alsa.pcm_status(c.card, "pcm0c"):
        servers = alsa.active_sound_servers()
        c.blocked("a PCM substream is already open -- the device cannot go "
                 f"idle while it is (active sound servers: {servers or 'none seen'})")

    # Enable the dev_dbg before the rebind, not after: the rebind's own probe
    # is one legitimate way for jockey3_idle_work() to end up armed, and if
    # a stray deactivation happened to land inside the settle window it
    # should be visible too, not just the one this case is explicitly
    # waiting for.
    ok, err = priv.dyndbg_ondemand(True)
    if not ok:
        c.blocked(f"cannot enable the on-demand dev_dbg: {err}")

    idx = c.card
    try:
        c.progress("forcing a fresh probe so the PCM rings are known to be "
                  "running before the idle wait starts (unbind/bind)")
        idx, err = rebind(settle)
        if idx is None:
            c.fail(f"could not force a fresh probe: {err}")
        else:
            node = alsa.rawmidi_node(idx)
            if not node:
                c.fail("no rawmidi device node found after rebind")
            else:
                ok, err = priv.set_idle_timeout(idle_timeout_s)
                if not ok:
                    c.fail(f"cannot set idle_timeout: {err}")
                else:
                    c.progress(f"idle_timeout set to {idle_timeout_s}s; "
                              f"waiting for the PCM rings to stop on their own")
                    marker = kmsg.Marker(f"{c.id}-wait")
                    marker.write()

                    stopped = wait_for_deactivation(c, marker,
                                                    idle_timeout_s + wait_margin_s)
                    c.metric("deactivations_observed", 1 if stopped else 0)
                    if not stopped:
                        c.progress(f"   no deactivation message seen within "
                                  f"{idle_timeout_s + wait_margin_s}s")
                        c.fail("the driver never logged stopping the PCM URB "
                              "rings after a fresh probe -- jockey3_idle_work() "
                              "did not fire as expected")
                    else:
                        c.progress("   driver confirmed: PCM URB rings "
                                  "stopped for idleness")

                        c.instruct(f"Generate MIDI IN traffic for "
                                  f"{watch_seconds}s: move a jog wheel, move "
                                  f"a fader or knob, press several buttons. "
                                  f"Which controls, or how many events, does "
                                  f"not matter -- keep going for the full "
                                  f"window. The PCM rings should stay "
                                  f"stopped throughout; only MIDI IN is "
                                  f"being exercised.")

                        total_bytes = watch_midi_in(c, node, watch_seconds)
                        c.metric("bytes_received_while_stopped", total_bytes)

                        if total_bytes == 0:
                            c.fail("no MIDI IN bytes arrived while the PCM "
                                  "URB rings were stopped -- THE GATE FAILS: "
                                  "the device does not deliver MIDI IN when "
                                  "its audio engine is idle, which closes "
                                  "on-demand streaming regardless of "
                                  "anything else in the design "
                                  "(re/on-demand_streaming.md)")
                        else:
                            c.progress(f"   {total_bytes} byte(s) received "
                                      f"while the PCM rings were stopped -- "
                                      f"MIDI IN survives the idle stop")
    finally:
        # Best-effort, mirrors pcm_n_sweep.py: leaving the message on is
        # harmless but every other case's dmesg.txt should not carry a line
        # this run turned on for its own purposes.
        priv.dyndbg_ondemand(False)
        priv.set_idle_timeout(600)
        # Phase 1 has no restart path (re/on-demand_streaming.md): whatever
        # just happened above, the PCM rings may now be stopped. A final
        # rebind leaves the target in the same streaming state every other
        # case already assumes, regardless of whether this one passed.
        _idx, err = rebind(settle)
        if err:
            c.note(f"final rebind to restore streaming did not confirm "
                  f"cleanly: {err} -- a later case may need a manual "
                  f"unbind/bind")

    c.done()


if __name__ == "__main__":
    main()
