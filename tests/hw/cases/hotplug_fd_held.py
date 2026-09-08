#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L3: JT-HOTPLUG-005 -- unplug while a stopped process holds a PCM fd.

The regression test for issue #37. The driver used to create its card with
snd_devm_card_new(), which ties it to the USB interface through devres:
snd_card_register() adds trigger_card_free() last, so on unbind it runs first
and calls snd_card_free(), which does not return until userspace has closed
every file descriptor on the card. On a physical unplug that unwind runs on the
USB hub work queue while holding the parent hub's device lock, so one process
that will not close its fd stalls hotplug for that whole hub subtree.

WHAT MAKES THIS A PREDICATE AND NOT A RATE
--------------------------------------------------------------------------
There is no race to catch here and nothing to accumulate over iterations. With
the bug the very first cycle wedges; with the fix it never does. A handful of
runs is the whole test -- see catalog.yaml for why the volume belongs to the
KASAN campaign instead.

The fd is held by a process stopped with SIGSTOP rather than merely a sleeping
one, so that the close cannot race the unplug and give a false pass on a timing
accident. A debugger, a SIGSTOP'd media server, or an application blocked in an
unrelated uninterruptible wait all reach the same state in the field.

WHAT IS AND IS NOT A DISCRIMINATOR
-----------------------------------------------
The card disappearing from /proc/asound is NOT evidence of anything: with the
bug, snd_card_free() runs snd_card_disconnect() before it starts waiting, so
the card goes away on time either way. What separates the two is whether the
hub can still work afterwards, so the predicate is the REPLUG:

  - fixed:  power the port back on, the device re-enumerates as normal.
  - buggy:  the hub work item is stuck in wait_for_completion(); nothing on
            that hub enumerates again, and a hung-task splat naming
            snd_card_free() appears after 120 s.

Powering the port itself may hang too, since uhubctl needs the hub's device
lock that the wedged work item holds. Every privileged call below therefore
carries an explicit timeout, and a timeout is reported as the wedge rather
than as a tool error.

THIS CASE CAN LEAVE THE MACHINE NEEDING MANUAL RECOVERY
------------------------------------------------------------
On a kernel that still has the bug, that is not a risk but the expected
outcome: USB on the affected controller stays dead until the wedged work item
is released, which nothing this case can do will achieve. Recovery is
`rmmod xhci_pci; modprobe xhci_pci` or a reboot. Deliberately not in the smoke
profile for that reason.

THE SECOND HALF: THE SLOT RELEASE MOVED
--------------------------------------------
Fixing #37 moved the card-slot release out of the devres unwind, which ran
before usb_unbind_interface() returned, and into card->private_free, which runs
at last close. So while the stopped process still holds the fd, the old card's
slot is legitimately still taken and a replug must come back at a DIFFERENT
index -- that is correct behavior, recorded as a metric, not a failure. What
would be a real defect is the slot never coming back: after the holder exits,
one more power cycle must land on the original index again. JT-PROBE-003 covers
slot reuse in the ordinary case; this covers it across the deferred free.
"""

import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402
from lib import alsa, priv         # noqa: E402

HOLDER = r"""
import os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_NONBLOCK)
sys.stdout.write("READY\n")
sys.stdout.flush()
while True:
    time.sleep(3600)
"""


def wait_for_card(present, timeout):
    """Wait for the card to appear or disappear. Returns (index, when|None)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        idx, _ = alsa.find_card()
        if (idx is not None) == present:
            return idx, time.time()
        time.sleep(0.05)
    idx, _ = alsa.find_card()
    return idx, None


def start_holder(c, index, timeout):
    """Open a PCM fd on the card in a child, then stop the child.

    Returns the Popen, already stopped, or None if it could not be started.
    """
    node = f"/dev/snd/pcmC{index}D0p"
    if not os.path.exists(node):
        c.blocked(f"no playback device node at {node}")

    proc = subprocess.Popen([sys.executable, "-c", HOLDER, node],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            err = (proc.stderr.read() or "").strip()[:200]
            c.blocked(f"could not open {node}: {err or 'holder exited'}")
        line = proc.stdout.readline()
        if line.strip() == "READY":
            os.kill(proc.pid, signal.SIGSTOP)
            return proc
    proc.kill()
    c.blocked(f"holder did not open {node} within {timeout}s")
    return None


def release_holder(proc):
    """Undo start_holder(), unconditionally. Safe to call more than once."""
    if proc is None or proc.poll() is not None:
        return
    try:
        os.kill(proc.pid, signal.SIGCONT)
    except ProcessLookupError:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def power(c, action, timeout, what):
    """Switch port power. A timeout here IS the wedge signature."""
    t0 = time.time()
    try:
        rc, _out, err = priv.usb_power(action, timeout=timeout)
    except Exception as exc:                      # noqa: BLE001
        c.fail(f"{what}: usb-power {action} raised after "
               f"{time.time() - t0:.0f}s ({exc}) -- the hub is not answering, "
               f"which is what a blocked unbind looks like from here")
        return False
    if rc != 0:
        c.fail(f"{what}: usb-power {action} failed after "
               f"{time.time() - t0:.0f}s: {(err or '').strip()[:120]}")
        return False
    return True


def main():
    c = Case()
    c.require_card()

    ok, why = priv.available()
    if not ok:
        c.blocked(f"cannot switch port power: {why}")
    if not priv.usb_power_available():
        c.blocked("no Jockey 3 behind a ppps-capable hub port")

    settle = float(c.params.get("settle_seconds", 15))
    off_seconds = float(c.params.get("off_seconds", 2))
    # Bounded on purpose: this is how long the case is willing to wait for a
    # hub that may never answer. Long enough that a slow but working hub is
    # not called a wedge, short enough not to sit there for the rest of the
    # run once it is one.
    wedge_timeout = float(c.params.get("wedge_timeout_seconds", 60))
    # The deferred free happens on the holder's close, but nothing in
    # userspace observes it directly -- the next enumeration's index does.
    free_settle = float(c.params.get("free_settle_seconds", 3))

    start_idx, _ = alsa.find_card()
    c.metric("card_index_first", start_idx)

    proc = None
    try:
        c.progress(f"holding a PCM fd on hw:{start_idx} from a stopped process")
        proc = start_holder(c, start_idx, timeout=30)

        c.progress("dropping port power with the fd still held")
        if not power(c, "off", wedge_timeout, "unplug"):
            return c.done()

        # Expected either way -- snd_card_disconnect() runs before the
        # blocking wait -- so this is a sanity check, not the predicate.
        _idx, gone = wait_for_card(False, settle)
        c.metric("card_gone_while_held", gone is not None)
        if gone is None:
            c.fail(f"card still present {settle}s after the port was powered "
                   f"down while an fd was held")

        time.sleep(off_seconds)

        # ---- the predicate ----
        c.progress("restoring port power -- a wedged unbind shows up here")
        t0 = time.time()
        if not power(c, "on", wedge_timeout, "replug"):
            c.note("usb-power did not return: consistent with a hub work item "
                   "blocked in snd_card_free(). Check dmesg for a hung-task "
                   "splat and expect to need rmmod/modprobe xhci_pci.")
            return c.done()

        held_idx, seen = wait_for_card(True, settle)
        c.metric("reenumerate_while_held_ms",
                 None if seen is None else round((seen - t0) * 1000, 1))
        if seen is None:
            c.fail(f"device did not re-enumerate within {settle}s of power "
                   f"being restored while a PCM fd was held -- the unbind did "
                   f"not complete (issue #37)")
            c.note("Expect a hung-task splat naming snd_card_free() after "
                   "120 s, and expect to need rmmod/modprobe xhci_pci.")
            return c.done()

        c.metric("card_index_while_held", held_idx)
        if held_idx == start_idx:
            # Not a failure: it only means the old card had already been
            # freed, i.e. something closed the fd. Worth recording, because it
            # means the second half of this case tested nothing.
            c.note(f"card came back at its original index hw:{held_idx} while "
                   f"the fd was still held -- the first card was freed early, "
                   f"so the slot-release half of this case did not apply")
        else:
            c.progress(f"re-enumerated at hw:{held_idx} while hw:{start_idx} "
                       f"is still held -- as expected")
    finally:
        release_holder(proc)

    # ---- the slot must come back once the holder lets go ----
    time.sleep(free_settle)
    c.progress("holder released; one more power cycle to check slot reuse")
    if not power(c, "off", wedge_timeout, "slot-reuse unplug"):
        return c.done()
    _idx, gone = wait_for_card(False, settle)
    if gone is None:
        c.fail(f"card still present {settle}s after the second power down")
        return c.done()
    time.sleep(off_seconds)
    if not power(c, "on", wedge_timeout, "slot-reuse replug"):
        return c.done()

    final_idx, seen = wait_for_card(True, settle)
    c.metric("card_index_last", final_idx)
    if seen is None:
        c.fail(f"device did not re-enumerate within {settle}s after the "
               f"holder was released")
        return c.done()

    subs = alsa.substreams(final_idx)
    for kind in ("playback", "capture", "rawmidi"):
        if not subs[kind]:
            c.fail(f"no {kind} substream after the final re-enumeration")

    if final_idx != start_idx:
        c.fail(f"card slot not reused after the deferred free: started at "
               f"hw:{start_idx}, ended at hw:{final_idx} -- the slot released "
               f"by card->private_free() did not come back")

    c.done()


if __name__ == "__main__":
    main()
