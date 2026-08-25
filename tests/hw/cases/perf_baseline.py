#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L3: baseline the host CPU/interrupt cost of driving the Jockey 3.

The measurement re/streaming_overhead.md is missing: how much interrupt load
and CPU time does this driver's continuous URB streaming actually cost, per
platform, per sample rate, idle and streaming? Everything in that study's
Part 6 experiment ladder (transfer coalescing, idle rate downshift) is a
before/after against the numbers this case produces.

1 + 2*len(params.rates) points, always sampled in this order:

  unbound     the driver detached from the device's interface -- no URBs
              submitted, nothing this driver does can be contributing. This
              is a proxy for "device unplugged", chosen over usb-power hub
              switching (JT-HOTPLUG's mechanism) because it needs no hub
              hardware and is available on every target, and because cutting
              bus power leaves the driver bound and repeatedly retrying
              against a vanished device -- which is its own noise source, not
              a clean floor.
  idle_R      for every R in params.rates: URBs running at R (this driver's
              normal resting state -- see jockey3.c's "URBs run free for the
              device's lifetime" DOC comment), nothing open --
              jockey3_silence_out_packet()'s memset path.
  stream_R    immediately after idle_R, at the same rate: playback and
              capture both open -- ploytec_encode_batch()'s codec path, plus
              aplay/arecord's own read()/write() syscall traffic.

idle_R and stream_R share the same completion rate (URBs run free regardless
of PCM open/close), so any cpu_pct_sys_irq_soft difference between them at a
given rate is specifically the per-frame codec/syscall cost, not per-URB
overhead -- which unbound vs. idle_R already isolates instead. This
decomposition is the point: transfer coalescing (streaming_overhead.md's
lever 4) only touches the per-URB share, so idle_R vs. stream_R is what tells
whether coalescing's completion-rate reduction will translate into a
proportional CPU saving, or a smaller one. A single unqualified "idle" point
measured once, at whatever rate the device happened to be left at, could not
make this comparison at every rate -- replaced 2026-08-26 for exactly that
reason.

At each point: HCD-line interrupts/s from /proc/interrupts (lib/perf.py,
not /proc/interrupts's aggregate line -- and not vmstat's aggregate 'in',
which cannot be attributed to this device at all), %sys+%irq+%soft CPU time
from /proc/stat, and ns/callback for jockey3_playback_callback and
jockey3_capture_callback via a function_graph trace restricted to those two
functions (root-owned tests/hw/priv/jockey3-testctl's trace-callbacks verb --
see lib/priv.py). All are read from unprivileged /proc files or the one
priv verb; no packages need installing for any of this.

NOT measured here: C-state residency (turbostat/powertop, x86-only, needs
root and a package -- linux-cpupower for turbostat, powertop for the other)
and the pi1test-style "board became unreachable" symptom, which is downstream
of this case's numbers, not a metric in its own right. Both are follow-ups,
not this case's job.

Metric-only, like JT-CODEC-005: a noisy or otherwise busy machine does not
fail this case, but every point's metrics carry a companion RSD-style
sanity note when the CPU-time sample looks unstable, and needs
`quiet-machine` for the same reason JT-CODEC-005 does.
"""

import math
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402
from lib import alsa, env, perf, priv    # noqa: E402

CHANNELS = 4                       # playback: Master L/R + Headphone L/R
CAPTURE_CHANNELS = 6               # fixed by the driver: min == max
FORMAT = "S24_3LE"
TRACE_FUNCTIONS = ("jockey3_playback_callback", "jockey3_capture_callback")
MARGIN_SECONDS = 5                 # extra runtime given to aplay/arecord -d
RESTORE_RATE = 44100               # the device's own power-on default


def start_stream_pair(device_out, device_in, rate, duration_s):
    """Launch continuous silent playback + capture at rate, for duration_s.

    Silence straight from /dev/zero -- no sox, no signal generation. This
    case only needs the URB stream running, not audio content: the load
    under study is per-packet, not per-sample-value.
    """
    duration = int(math.ceil(duration_s)) + MARGIN_SECONDS
    playback = subprocess.Popen(
        ["aplay", "-D", device_out, "-r", str(rate), "-c", str(CHANNELS),
         "--format", FORMAT, "-d", str(duration), "/dev/zero"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    capture = subprocess.Popen(
        ["arecord", "-D", device_in, "-r", str(rate), "-c",
         str(CAPTURE_CHANNELS), "--format", FORMAT, "-t", "raw",
         "-d", str(duration), "/dev/null"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    return playback, capture


def stop_stream_pair(procs):
    for p in procs:
        if p is None:
            continue
        if p.poll() is None:
            p.terminate()
    for p in procs:
        if p is None:
            continue
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=5)


def start_trace(c, name):
    rc, _out, err = priv.trace_callbacks_start()
    if rc != 0:
        c.note(f"{name}: trace-callbacks start failed rc={rc}: "
               f"{(err or '').strip()[:200]}")
        return False
    return True


def collect_trace(c, name, active):
    if not active:
        return {}
    rc, out, err = priv.trace_callbacks_collect()
    if rc != 0:
        c.note(f"{name}: trace-callbacks collect failed rc={rc}: "
               f"{(err or '').strip()[:200]}")
        return {}
    return perf.parse_function_graph(out, TRACE_FUNCTIONS)


def sample_point(c, name, hcd, settle_seconds, sample_seconds):
    """Sample IRQ/CPU/callback timing for sample_seconds, after settling.

    The caller has already brought the device into the state being measured
    (unbound / idle / streaming) before this runs.
    """
    time.sleep(settle_seconds)

    traced = start_trace(c, name)
    irq_before = perf.read_hcd_irq_total(hcd)
    stat_before = perf.read_cpu_stat()
    t0 = time.monotonic()
    time.sleep(sample_seconds)
    elapsed = time.monotonic() - t0
    irq_after = perf.read_hcd_irq_total(hcd)
    stat_after = perf.read_cpu_stat()
    durations = collect_trace(c, name, traced)

    metrics = {}
    if irq_before is not None and irq_after is not None and elapsed > 0:
        metrics["irq_per_s"] = round((irq_after - irq_before) / elapsed, 1)
    else:
        c.note(f"{name}: no HCD line matched in /proc/interrupts "
               f"(host_controller={hcd!r}) -- irq_per_s not recorded")

    cpu_pct = perf.cpu_pct_sys_irq_soft(stat_before, stat_after)
    if cpu_pct is not None:
        metrics["cpu_pct_sys_irq_soft"] = cpu_pct

    for fn in TRACE_FUNCTIONS:
        values = durations.get(fn) or []
        short = "playback" if "playback" in fn else "capture"
        if values:
            metrics[f"ns_per_{short}_cb"] = round(perf.mean(values), 1)
            metrics[f"{short}_cb_calls"] = len(values)
        elif traced:
            c.note(f"{name}: traced but recovered 0 calls for {fn} -- "
                   f"see the raw trace saved for this point")

    return metrics, durations


def set_device_rate(c, device, rate, label):
    """Trigger a rate change with a brief silent playback open, then close.

    Used both to bring the device to a rate before an idle_R point (URBs run
    free at whatever rate was last set, regardless of PCM open/close --
    jockey3.c's "URBs run free for the device's lifetime" DOC comment -- so
    this is the only way to control which rate idle_R actually measures) and
    to restore a low resting rate before the case exits (restore_resting_rate()
    below).

    Returns True on success. A failure is reported with c.fail(), not
    c.note(): 2026-08-26 on pi1test, this exact operation (there, the final
    restore) timed out right after a 96 kHz point at 95% CPU, and the failure
    sat in a JSON note field while the operator diagnosed and manually
    power-cycled the device -- exactly the fault this driver holds itself to
    never hiding (jockey3.c's fault-handling convention). kill() only
    delivers SIGKILL; it does not free a process blocked in an
    uninterruptible kernel wait (D state) -- pi1test has a documented
    arecord stuck like that for ~46 minutes
    (re/pi1test_platform_notes.md, 2026-08-25) -- so the post-kill wait is
    bounded too, rather than risking the same wedge here.
    """
    p = subprocess.Popen(
        ["aplay", "-D", device, "-r", str(rate), "-c", str(CHANNELS),
         "--format", FORMAT, "-d", "1", "/dev/zero"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        _out, err = p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            c.fail(f"{label}: aplay would not die after SIGKILL -- likely "
                   f"stuck in an uninterruptible kernel wait, not just slow. "
                   f"Device is left at an unknown rate; may need manual "
                   f"recovery (see re/pi1test_platform_notes.md)")
            return False
        c.fail(f"{label}: timed out after 10s -- device may still be at "
               f"whatever rate it was at before")
        return False
    if p.returncode != 0:
        c.fail(f"{label}: aplay exited {p.returncode}: "
               f"{(err or '').strip()[:200]}")
        return False
    return True


def restore_resting_rate(c, device, rate):
    """Bring the device back to a low, cheap rate before the case exits.

    URBs run free for the device's lifetime regardless of PCM open/close, so
    whatever rate the last streaming point used is exactly what the device
    keeps blasting at afterwards -- forever, until something else changes it.
    On a weak host that is not a cosmetic loose end: the 2026-08-26 pi1test
    run of this case left the board parked at 96 kHz once its own measurement
    was done, at ~96% sys+irq+soft, and that persisted until the operator
    power-cycled the device by hand (documented in
    re/pi1test_platform_notes.md's 2026-08-25 entry as the same fix, done
    manually, for the same symptom). This is the automated version of that
    fix, run unconditionally so a JT-PERF-001 run does not leave the rig
    worse off than it found it.
    """
    c.progress(f"restoring the device to {rate} Hz before finishing")
    set_device_rate(c, device, rate, label=f"restoring to {rate} Hz")


def save_raw_trace(c, name, durations):
    if not durations:
        return
    path = os.path.join(c.workdir, f"trace_{name}.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            for fn, values in durations.items():
                f.write(f"{fn}: {len(values)} calls\n")
    except OSError:
        pass


def main():
    c = Case()
    c.require_card()
    c.require_tools("aplay", "arecord")

    rates = c.params.get("rates", [44100, 48000, 88200, 96000])
    sample_seconds = float(c.params.get("sample_seconds", 10))
    settle_seconds = float(c.params.get("settle_seconds", 2))
    skip_unbound = bool(c.params.get("skip_unbound", False))

    usb_info = env.usb_device_info()
    hcd = usb_info.get("host_controller") if usb_info else None
    if not hcd:
        c.note("host controller driver not resolved from sysfs -- "
               "irq_per_s will not be recorded at any point")

    device = c.device or alsa.device_name(c.card)

    all_metrics = {}

    # ---------------------------------------------------------- unbound
    if skip_unbound:
        c.note("skip_unbound=true; the unbound baseline was not measured")
    else:
        c.progress("unbound: detaching the driver for the host-floor baseline")
        ok, why = priv.available()
        if not ok:
            c.note(f"cannot reach jockey3-testctl, unbound point skipped: {why}")
        else:
            rc, _out, err = priv.unbind()
            if rc != 0:
                c.note(f"unbind failed, unbound point skipped: "
                       f"{(err or '').strip()[:200]}")
            else:
                metrics, durations = sample_point(
                    c, "unbound", hcd, settle_seconds, sample_seconds)
                all_metrics["unbound"] = metrics
                save_raw_trace(c, "unbound", durations)
                rc, _out, err = priv.bind()
                if rc != 0:
                    c.fail(f"could not rebind the driver after the unbound "
                           f"point: {(err or '').strip()[:200]}")
                    c.done()
                if not alsa.wait_for_card_live(c.card, timeout=10.0):
                    c.fail("card did not come back live after rebind")
                    c.done()

    # ------------------------------------------------- idle_R / stream_R
    #
    # For each rate, two points back to back: idle_R (URBs running at this
    # rate, nothing open -- jockey3_silence_out_packet()'s memset path) then
    # stream_R (a real substream open -- ploytec_encode_batch()'s codec path
    # plus aplay/arecord's own read()/write() syscall traffic). Both share
    # the same irq_per_s at a given rate (URBs run free regardless of PCM
    # open/close), so any cpu_pct_sys_irq_soft difference between the two is
    # specifically the per-frame codec/syscall cost -- not per-URB overhead,
    # which idle_R vs. unbound already isolates. Kept adjacent in time
    # (rather than all idle_R then all stream_R) so the two share as much of
    # the same thermal/load state as possible. This is what tells lever 4
    # (transfer coalescing, which only touches the per-URB share) apart from
    # lever 2/3 (which also touch the per-frame share, but only while idle).
    # A single unqualified "idle" point measured once, at whatever rate the
    # device happened to be left at, could not make this comparison at every
    # rate -- this replaced that design 2026-08-26.
    try:
        for rate in rates:
            if set_device_rate(c, device, rate, label=f"setting {rate} Hz"):
                name = f"idle_{rate}"
                c.progress(f"{name}: driver bound, URBs running at "
                           f"{rate} Hz, nothing open")
                if not alsa.wait_for_card_live(c.card, timeout=10.0):
                    c.fail(f"card not live for {name}")
                    c.done()
                metrics, durations = sample_point(
                    c, name, hcd, settle_seconds, sample_seconds)
                all_metrics[name] = metrics
                save_raw_trace(c, name, durations)
            else:
                c.note(f"idle_{rate} skipped: could not set {rate} Hz")

            name = f"stream_{rate}"
            c.progress(f"{name}: playback + capture open at {rate} Hz")
            duration_s = settle_seconds + sample_seconds
            procs = start_stream_pair(device, device, rate, duration_s)
            # A device that refuses this rate fails fast, well inside the
            # settling time below -- catch that here rather than measuring a
            # stream that never actually opened. Once past this point every
            # process is deliberately cut short by stop_stream_pair() when
            # the sample window ends, so no exit-code check is meaningful
            # after it.
            time.sleep(0.5)
            for p, label in zip(procs, ("aplay", "arecord")):
                if p.poll() is not None and p.returncode != 0:
                    _out, err = p.communicate()
                    c.note(f"{name}: {label} exited {p.returncode} before "
                           f"streaming started: {(err or '').strip()[:200]}")
            try:
                metrics, durations = sample_point(
                    c, name, hcd, settle_seconds, sample_seconds)
            finally:
                stop_stream_pair(procs)
            all_metrics[name] = metrics
            save_raw_trace(c, name, durations)
    finally:
        # Unconditional, even on an exception mid-sweep: whatever rate the
        # last completed point used is what the device keeps running at
        # indefinitely once this process exits -- see
        # restore_resting_rate()'s docstring.
        if not bool(c.params.get("skip_restore_rate", False)):
            restore_resting_rate(
                c, device, int(c.params.get("restore_rate", RESTORE_RATE)))

    # ------------------------------------------------------------- report
    for point, metrics in all_metrics.items():
        for key, value in metrics.items():
            c.metric(f"{key}_{point}", value)

    report_points = ["unbound"]
    for rate in rates:
        report_points += [f"idle_{rate}", f"stream_{rate}"]
    for point in report_points:
        m = all_metrics.get(point, {})
        irq = m.get("irq_per_s", "?")
        cpu = m.get("cpu_pct_sys_irq_soft", "?")
        pb = m.get("ns_per_playback_cb", "?")
        cap = m.get("ns_per_capture_cb", "?")
        c.progress(f"{point:>12}: irq={irq}/s  cpu={cpu}%  "
                   f"playback_cb={pb}ns  capture_cb={cap}ns")

    c.done()


if __name__ == "__main__":
    main()
