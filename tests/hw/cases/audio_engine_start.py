#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L3: does the device's audio engine actually start after a power cycle?

THE FAILURE THIS EXISTS FOR
---------------------------
2026-08-13, production kernel: the device enumerated, the driver probed
cleanly, the rate was programmed and verified, ALSA opened a playback stream
and accepted every sample -- and nothing came out of the device. Three power
cycles in a row, after one that worked. Nothing in dmesg, no xrun, no error
return anywhere. Every counter said the run was fine.

The OpenVizsla trace showed what had happened. Playback was flawless: 100% of
packets carried non-zero audio at the correct cadence, with the MIDI idle byte
at offset 480 and the sync byte at 481 in every one. But the capture endpoint
returned nothing but zeros -- 82722 packets, not one non-zero byte, against
95.5% non-zero in the cycle that worked. The device was accepting the stream
and never starting its audio engine.

The control plane was byte-identical between the working and failing cycles:
same requests in the same order, same status bytes, same rate programmed and
read back, same inter-transfer gaps. So this is not something the driver can
see by checking return codes, and it will not show up in any existing case.

WHY SILENCE IS THE SIGNAL HERE, AND NOT IN JT-PCM-004
-----------------------------------------------------
JT-PCM-004 captures at each rate and deliberately does NOT fail on silence,
because with nothing patched into the inputs silence is the correct answer.
That is a different question from this one.

A running ADC is never bit-exact silent. It produces dither and noise floor
whether or not anything is connected, which is why the working cycles measured
95.5% and 99.4% of packets non-zero with nothing plugged in. Individual
all-zero packets are normal -- a few percent of them. A capture in which *no
sample anywhere* is non-zero is not a quiet input; it is a converter that is
not running.

So the threshold is deliberately nowhere near the observed values: anything
below `min_nonzero_fraction` (default 1%) fails, against ~0% when broken and
>95% when working. There is no sensitivity to tune and no risk of a quiet
studio failing the case.

IT TAKES A DEVICE POWER CYCLE, AND THAT IS THE FINDING
-----------------------------------------------------
Measured 2026-08-13: ten consecutive re-enumerations driven through the ppps
hub switch, and the device came back correctly every time. Not one silent
cycle. Ten mains power cycles through the switch, the same evening, and every
single one came back silent -- nine measured, 0.0000% non-zero in all of them,
playback confirmed dead by ear afterwards.

That the switched cycle fails 100% of the time where hand-flicking the power
failed three times in four is itself evidence. The switch holds power off for
a measured five seconds and breaks it cleanly; a hand on a rocker is quicker
and leaves residual charge. The more completely the device is cold-booted, the
more reliably this happens -- which is what "the driver starts talking before
the device has finished booting" predicts, and the opposite of what a flaky
cable or a marginal supply would do.

The Jockey 3 is self-powered (bmAttributes 0xc0), so cutting VBUS at the hub
is a cable unplug and not a device power-off, and evidently the two are not
equivalent to the device. That fits the rest of what the traces showed: a USB
re-enumeration does send the rate back to 44100 Hz and the status byte back to
0x12, but the control surface LEDs keep their pattern across an unplug, so the
firmware is doing a partial re-initialization rather than a cold boot.

So the device has two distinct levels of reset, and only the deeper one
provokes this. The failure is a race against the device's own power-on boot:
its USB core answers early enough to enumerate and be programmed, while
whatever runs its audio engine is still coming up. Program it in that window
and the engine never starts -- which is why a debug kernel, which is slower to
reach probe, has never shown the fault.

The practical consequence is that this case cannot be automated through the
hub. `device_power` is the mode that reproduces it, and it switches the
device's own supply: a network relay if one is configured (see lib/power/),
otherwise an operator working the switch by hand. With a relay it runs
unattended, which is what makes a fault this intermittent practical to chase
-- three failures in four cycles, but only after a real cold boot.

Both take the same four beats, and the operator is not a special case: take
the device down, watch the bus confirm it went, hold it down for a measured
interval, put it back. The operator gets two prompts rather than one because
the hold is the part that matters -- reconnect before the supply has drained
and the boot is warm, which is the case that already works -- and a person
asked to "switch it off, wait, and switch it back on" is timing that hold
themselves. The machine times it instead, and asks for the second action only
once the first has been observed.

`usb_power` is kept anyway: ten clean cycles is a useful regression baseline,
and if it ever starts failing that is a different and worse defect.

`module_reload` leaves the device alone and re-probes an already-running one.
It exists because of the one measurable difference between the working and
failing cycles in the 2026-08-13 trace: probe ran 43.9 ms after
SET_CONFIGURATION in the cycle that worked and 0.1 ms after it in the ones
that did not, because the working cycle probed from modprobe rather than from
enumeration.

WHAT A FAILURE HERE MEANS
-------------------------
Not that the driver returned an error -- it will not have. It means the device
was left in a state where it accepts USB traffic and produces no audio, and
the driver did not notice. Report `silent_cycles` and `first_silent_cycle`;
the run that produced them is worth an OpenVizsla trace.
"""

import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402
from lib import alsa, kmsg, power, priv    # noqa: E402

CHANNELS = 6                       # fixed by the driver: min == max
FORMAT = "S24_3LE"
BYTES_PER_SAMPLE = 3


def nonzero_fraction(path):
    """Fraction of frames in a raw S24_3LE capture carrying any non-zero byte.

    Frame-granular rather than sample-granular on purpose: the question is
    whether the converter is running at all, and a frame is the smallest unit
    that answers it. Reading the whole file matters -- a device that starts
    late would look dead from a prefix alone.
    """
    frame = CHANNELS * BYTES_PER_SAMPLE
    total = live = 0
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(frame * 8192)
                if not chunk:
                    break
                for off in range(0, len(chunk) - frame + 1, frame):
                    total += 1
                    if any(chunk[off:off + frame]):
                        live += 1
    except OSError:
        return None, 0
    if not total:
        return None, 0
    return live / total, total


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


def wait_for_substreams(idx, timeout):
    """Poll until the card is not just present but openable.

    Two separate events, and conflating them cost a cycle. The PCM substreams
    appearing in /proc/asound says probe created the devices; the character
    devices under /dev/snd appearing says udev has caught up and arecord can
    actually open them. Cycle 10 of the first ten-cycle run failed with "Cannot
    get card index for 1" having passed the /proc check in 0.1 ms -- the card
    was there and the node was not.

    Existence is not the test, though, and assuming it was cost two more runs.
    A node can be present and dead: /dev/snd entries from the previous
    incarnation survive a disconnect until udev reaps them, which is
    asynchronous and slower than probe. Worse, the card they belong to is
    indistinguishable by name from the new one, because /proc/asound/card<idx>
    and its "id" file are created by snd_devm_card_new() and snd_card_set_id()
    -- both of which run early in probe, before the device is initialized and
    long before snd_card_register() publishes the character devices. So
    find_card() can return an index for a card that does not yet exist as far
    as userspace is concerned, and os.path.exists() then passes against the
    stale nodes of its predecessor.

    That window used to be sub-millisecond and nothing noticed. Adding a
    settling delay to probe widened it to seconds and it failed immediately.

    The fix is to open the control node rather than stat it: opening the stale
    node of a departed card fails with ENODEV, and the new one does not appear
    until registration. Deliberately the *control* device and not a PCM one --
    opening capture would run the driver's deferred capture-recovery path and
    perturb the very thing the case measures.
    """
    t0 = time.time()
    deadline = t0 + timeout
    nodes = (f"/dev/snd/pcmC{idx}D0c", f"/dev/snd/pcmC{idx}D0p")
    while True:
        subs = alsa.substreams(idx)
        missing = [w for w in ("playback", "capture") if not subs[w]]
        missing += [n for n in nodes if not os.path.exists(n)]
        if not alsa.control_is_live(idx):
            missing.append(f"/dev/snd/controlC{idx} (not openable)")
        if not missing or time.time() >= deadline:
            return round((time.time() - t0) * 1000, 1), missing
        time.sleep(0.02)


class Actuator:
    """Take the device away, then bring it back.

    Every trigger is the same four beats -- remove it, watch it go, hold, put
    it back -- and only the mechanism differs. Keeping that sequence in one
    place is what lets the operator path behave exactly like the relay path
    instead of being a special case bolted on: the operator is asked to switch
    off, the bus confirms it, the hold is timed by the machine rather than by
    somebody counting, and only then are they asked to switch on.

    In every mode the confirmation is the device disappearing from and
    reappearing on the bus. Nothing here trusts an actuator's own report, an
    operator's word included.
    """

    kind = "?"
    timeout = 8.0
    gone_hint = ""
    back_hint = ""

    def off(self):
        raise NotImplementedError

    def on(self):
        raise NotImplementedError


class DevicePower(Actuator):
    """The device's own mains supply: a true cold boot, unattended.

    Whether that is a relay, a solid-state switch or something else is
    lib/power/'s business, not this case's."""

    kind = "device-power"
    gone_hint = "is the switch feeding the outlet the device is plugged into?"

    def __init__(self, timeout):
        self.timeout = timeout

    def off(self):
        return power.set_state(False)

    def on(self):
        return power.set_state(True)


class OperatorPower(Actuator):
    """The same cold boot, asked for rather than switched.

    Deliberately two prompts, not one. Asking somebody to "switch it off, wait,
    and switch it back on" leaves the length of the hold to them, and the hold
    is the part that matters -- reconnect too early and the boot is warm, which
    is the case that already works. Splitting it lets the machine time the
    hold and confirm the first step before requesting the second.
    """

    kind = "operator"
    gone_hint = "was the device switched off?"
    back_hint = "was the device switched back on?"

    def __init__(self, case, timeout):
        self.case = case
        self.timeout = timeout

    def off(self):
        self.case.instruct("Switch the device OFF, and wait for its LEDs to "
                           "go dark.")
        return True, "asked"

    def on(self):
        self.case.instruct("Switch the device ON again.")
        return True, "asked"


class UsbPower(Actuator):
    """VBUS at the hub port. A cable unplug for a self-powered device, not a
    power-off -- kept as a regression baseline, not as a way to reproduce."""

    kind = "usb-power"
    gone_hint = "did the hub actually drop the port?"

    def __init__(self, timeout):
        self.timeout = timeout

    def _switch(self, action):
        rc, _out, err = priv.usb_power(action)
        if rc != 0:
            return False, f"usb-power {action} failed: {(err or '').strip()[:120]}"
        return True, action

    def off(self):
        return self._switch("off")

    def on(self):
        return self._switch("on")


class ModuleReload(Actuator):
    """Driver only. The device stays powered and enumerated; the card goes."""

    kind = "module-reload"
    gone_hint = "did the module actually unload?"

    def __init__(self, timeout):
        self.timeout = timeout

    def off(self):
        rc, _out, err = priv.unload_module()
        return (rc == 0), (f"unload failed: {str(err).strip()[:120]}"
                           if rc else "unloaded")

    def on(self):
        rc, _out, err = priv.load_module()
        return (rc == 0), (f"load failed: {str(err).strip()[:120]}"
                           if rc else "loaded")


# dmesg line prefix, e.g. "[69523.605737] ". The kernel stamps the driver's
# own messages from the same clock, which is the whole point of reading timing
# from here rather than from time.time() in this process.
KTIME_RE = re.compile(r"^\[\s*(\d+\.\d+)\]")

# The device announcing itself on the bus, and the driver's first control
# transfer. "Firmware ... v..." is dev_info, so it survives a production kernel
# with no dynamic debug -- which is exactly the configuration the cold-boot
# fault needs. It is logged by ploytec_get_firmware(), the first EP0 transfer
# of the sequence, so its timestamp is when the driver first spoke to the
# device.
ENUM_RE = re.compile(r"usb [\d.-]+: new .* USB device number")
FIRST_EP0_RE = re.compile(r"Firmware 0x[0-9a-f]+ v")


def _ktime(line):
    m = KTIME_RE.match(line.strip())
    return float(m.group(1)) if m else None


def cycle_timings(lines, marks):
    """Per-cycle timings, in kernel time, from a marker to the events after it.

    For each cycle this yields how long the device took to enumerate after
    power was restored, and how long after enumerating before the driver sent
    its first control transfer. The second number is the one the settling-delay
    bisect is trying to establish, and it cannot be measured from the harness:
    the driver's first transfer is not an event userspace can see.

    Anything not found is left out rather than reported as zero. A missing
    number here means the log did not contain the event, which is worth
    noticing, and a zero would hide it.
    """
    out = {}
    for i, mark in marks:
        if not mark.written:
            continue
        start = None
        for n, line in enumerate(lines):
            if mark.token in line:
                start = n
        if start is None:
            continue
        t_mark = _ktime(lines[start])
        if t_mark is None:
            continue
        t_enum = t_ep0 = None
        for line in lines[start + 1:]:
            ts = _ktime(line)
            if ts is None:
                continue
            if t_enum is None and ENUM_RE.search(line):
                t_enum = ts
            elif t_enum is not None and FIRST_EP0_RE.search(line):
                t_ep0 = ts
                break
        if t_enum is not None:
            out[f"power_on_to_enumerate_ms_{i}"] = round((t_enum - t_mark) * 1000, 1)
        if t_enum is not None and t_ep0 is not None:
            out[f"enumerate_to_first_ep0_ms_{i}"] = round((t_ep0 - t_enum) * 1000, 1)
            out[f"power_on_to_first_ep0_ms_{i}"] = round((t_ep0 - t_mark) * 1000, 1)
    return out


def reenumerate(act, off_seconds, mark=None):
    """Remove the device, hold it away, bring it back.

    Returns (card_index, elapsed_ms, None), or (None, None, reason). The reason
    is handed back rather than recorded here so the caller can print its
    per-cycle progress line first: the runner reads the LAST line of stderr as
    the failure reason, so anything emitted after c.fail() would replace it.

    The bus is the witness at both ends.

    `mark` is written to the kernel log immediately before power is restored,
    so that the interval from power-on to the driver's first control transfer
    can be read off the kernel clock rather than measured here -- see
    cycle_timings().
    """
    t0 = time.time()

    ok, detail = act.off()
    if not ok:
        return None, None, f"could not take the device down ({act.kind}): {detail}"

    _idx, gone = wait_for_card(False, act.timeout)
    if gone is None:
        return None, None, (
            f"card still present {act.timeout:g}s after being taken down "
            f"({act.kind})" + (f" -- {act.gone_hint}" if act.gone_hint else ""))

    time.sleep(off_seconds)

    if mark is not None:
        mark.write()

    ok, detail = act.on()
    if not ok:
        return None, None, f"could not bring the device back ({act.kind}): {detail}"

    idx, seen = wait_for_card(True, act.timeout)
    if seen is None:
        return None, None, (
            f"card did not come back within {act.timeout:g}s ({act.kind})"
            + (f" -- {act.back_hint}" if act.back_hint else ""))
    return idx, round((seen - t0) * 1000, 1), None


def main():
    c = Case()
    c.require_card()
    c.require_tools("arecord")

    ok, why = priv.available()
    if not ok:
        c.blocked(f"privileged helper unavailable: {why}")

    iterations = int(c.params.get("iterations_per_run", 10))
    # Long enough for the device's own supply to drain: the point is a cold
    # boot, and reconnecting mains too early gives a warm one, which is the
    # case that already works.
    off_seconds = float(c.params.get("off_seconds", 5))
    settle = float(c.params.get("settle_seconds", 8))
    seconds = int(c.params.get("seconds", 2))
    rate = int(c.params.get("rate", 44100))
    floor = float(c.params.get("min_nonzero_fraction", 0.01))
    operator_timeout = float(c.params.get("operator_timeout_seconds", 120))

    trigger = c.params.get("trigger", "device_power")
    if trigger not in ("device_power", "usb_power", "module_reload"):
        c.blocked(f"unknown trigger {trigger!r}")
    if trigger == "usb_power" and not priv.usb_power_available():
        c.blocked("no Jockey 3 behind a ppps-capable hub port")
    if trigger == "device_power":
        # A relay makes this unattended; without one it needs somebody at the
        # switch. Either satisfies the case, so which one is in play is
        # recorded rather than required.
        if power.available():
            act = DevicePower(settle)
        elif c.attended:
            act = OperatorPower(c, operator_timeout)
        else:
            c.blocked("device_power needs either a configured mains switch "
                      "(see lib/power/) or an operator at the switch")
    elif trigger == "usb_power":
        act = UsbPower(settle)
    else:
        act = ModuleReload(settle)
    c.metric("power_control", act.kind)

    c.metric("trigger", trigger)
    c.metric("rate", rate)

    fractions, enumerate_ms = [], []
    silent_cycles = 0
    first_silent = None
    cycles = 0
    marks = []

    # One line per cycle, because this case runs for two minutes with nothing
    # to show for it otherwise and dmesg was the only way to tell it was alive.
    # Every exit from a cycle reports before c.fail(), never after: the runner
    # takes the LAST line of stderr as the failure reason, so a progress line
    # emitted afterwards would silently become the verdict.
    def report(i, verdict, detail):
        c.progress(f"    cycle {i}/{iterations}  {verdict:4}  {detail}")

    for i in range(1, iterations + 1):
        # Checked before the cut, never in the middle of one: a cycle that has
        # removed the device's power is half-finished, and the useful place to
        # stop is on a boundary with the device up.
        if c.stop_requested():
            c.note(f"stopped early by request after {i - 1} of {iterations} cycles")
            break
        c.status(f"    cycle {i}/{iterations}  ....  power cycling")
        mark = kmsg.Marker(f"{c.id}#cycle{i}")
        marks.append((i, mark))
        idx, took, why = reenumerate(act, off_seconds, mark)
        if idx is None:
            report(i, "FAIL", why)
            c.fail(f"cycle {i}: {why}")
            break
        enumerate_ms.append(took)

        c.status(f"    cycle {i}/{iterations}  ....  waiting for the card")
        settle_ms, missing = wait_for_substreams(idx, settle)
        if missing:
            report(i, "FAIL", f"no {', '.join(missing)} after {settle:g}s")
            c.fail(f"cycle {i}: no {', '.join(missing)} substream after "
                   f"re-enumeration")
            break

        device = alsa.device_name(idx)
        raw = os.path.join(c.workdir, f"engine_{i}.raw")
        c.status(f"    cycle {i}/{iterations}  ....  capturing {seconds}s")
        try:
            p = subprocess.run(
                ["arecord", "-D", device, "-d", str(seconds), "-f", FORMAT,
                 "-c", str(CHANNELS), "-r", str(rate), "-t", "raw", raw],
                capture_output=True, text=True, timeout=seconds + 30)
            rc, err = p.returncode, p.stderr
        except subprocess.TimeoutExpired:
            rc, err = 124, "arecord did not finish"
        if rc != 0:
            report(i, "FAIL", f"arecord exited {rc}")
            c.fail(f"cycle {i}: arecord exited {rc}: "
                   f"{(err or '').strip()[:120]}")
            break

        frac, frames = nonzero_fraction(raw)
        cycles = i
        if frac is None:
            report(i, "FAIL", "captured nothing to measure")
            c.fail(f"cycle {i}: captured nothing to measure")
            break
        fractions.append(round(frac, 4))

        report(i, "FAIL" if frac < floor else "pass",
               f"{frac:7.2%} non-zero of {frames} frames, "
               f"card ready in {settle_ms:g} ms")

        if frac < floor:
            silent_cycles += 1
            if first_silent is None:
                first_silent = i
            c.fail(f"cycle {i}: capture is bit-exact silent over {frames} "
                   f"frames ({frac:.4%} non-zero, floor {floor:.0%}) -- the "
                   f"device accepted the stream without starting its audio "
                   f"engine. Nothing in dmesg will say so.")
        else:
            # Keep the evidence only for the cycles that failed.
            try:
                os.unlink(raw)
            except OSError:
                pass

        c.metric(f"pcm_settle_ms_{i}", settle_ms)
        # Per cycle, not just the run's min/max: a partial run near the
        # fall-over is the interesting case, and an aggregate cannot say which
        # cycles were silent. See re/usb/settling_delay_dataset.py.
        c.metric(f"nonzero_fraction_{i}", round(frac, 4))

    c.metric("cycles", cycles)
    c.metric("silent_cycles", silent_cycles)
    c.metric("first_silent_cycle", first_silent)
    if fractions:
        c.metric("nonzero_fraction_min", min(fractions))
        c.metric("nonzero_fraction_max", max(fractions))
    if enumerate_ms:
        c.metric("enumerate_ms_max", max(enumerate_ms))

    # Timing read off the kernel clock rather than measured here. enumerate_ms
    # above stops when /proc/asound/card<idx> appears, which is early in probe
    # and not when the card becomes usable, so it under-reports; and the
    # driver's first control transfer is not an event userspace can observe at
    # all. Both are visible in the log, stamped by the same clock as the
    # driver's own messages.
    for key, value in cycle_timings(kmsg.read_log(), marks).items():
        c.metric(key, value)

    if silent_cycles:
        c.note(f"{silent_cycles} of {cycles} cycles produced a device that "
               f"accepted playback and generated no audio. The raw captures "
               f"for those cycles are kept in the work directory. This run is "
               f"worth an OpenVizsla trace -- see "
               f"re/usb/init_timing_comparison.md.")
    c.done()


if __name__ == "__main__":
    main()
