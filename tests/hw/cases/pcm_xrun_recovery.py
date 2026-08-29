#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""JT-PCM-009 (playback) / JT-PCM-011 (capture): deterministic xrun
recovery via /proc's xrun_injection.

No case in this suite specifically exercises the driver's xrun-recovery
path -- jockey3_pcm_prepare()'s liveness check, which ALSA core runs on
every xrun recovery -- because none of them produce an xrun on purpose.
CONFIG_SND_PCM_XRUN_DEBUG's xrun_injection proc file lets this one force
real ones, at chosen moments, on an otherwise healthy stream: a single
write calls snd_pcm_stop_xrun() on the open substream directly, the same
core function a genuine buffer underrun/overrun reaches. aplay/arecord
recover from that the normal way -- prepare and continue -- without this
case doing anything special.

This exercises the FAST path only: this driver's URBs run free of PCM
trigger state (see the top-of-file DOC in jockey3.c), so an ALSA-core-only
xrun leaves them untouched, jockey3_check_urb_stream_alive() finds the
stream alive immediately, and jockey3_pcm_prepare()'s liveness-check/
recovery branch (JOCKEY3_STREAM_STARTUP_GRACE_MS) is never reached. There
is no userspace-safe way to force a genuine URB-level stall on demand to
exercise that branch too; that class of event is covered statistically by
rate-change soak runs instead (re/rate_change_stall.md).

    pcm_xrun_recovery.py playback | capture
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402
from lib import alsa, priv         # noqa: E402

PLAYBACK_CHANNELS = 4               # fixed by the driver: Master + Headphone
CAPTURE_CHANNELS = 6                # fixed by the driver: min == max
FORMAT = "S24_3LE"

# How long to give watch_pcm's xrun counter to rise after one injection
# before treating it as "did not land" -- generous relative to watch_pcm's
# 20ms poll interval, so only a genuine miss (the write reached a substream
# that was not open and running) counts as one.
INJECT_CONFIRM_S = 1.0


def _start(direction, device, rate, seconds, channels, capture_path):
    if direction == "playback":
        gen = subprocess.Popen(
            ["sox", "-n", "-r", str(rate), "-c", str(channels), "-b", "24",
             "-e", "signed-integer", "-t", "raw", "-",
             "synth", str(seconds), "sine", "E3", "gain", "-6"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        p = subprocess.Popen(
            ["aplay", "-D", device, "-r", str(rate), "-c", str(channels),
             "--format", FORMAT, "-t", "raw"],
            stdin=gen.stdout, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True)
        gen.stdout.close()
        return p, gen
    p = subprocess.Popen(
        ["arecord", "-D", device, "-r", str(rate), "-c", str(channels),
         "--format", FORMAT, "-t", "raw", "-d", str(int(round(seconds)) + 1),
         capture_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    return p, None


def _finish(p, gen, seconds):
    try:
        _out, err = p.communicate(timeout=seconds + 30)
    except subprocess.TimeoutExpired:
        p.kill()
        if gen:
            gen.kill()
        return 124, "did not finish within timeout"
    if gen:
        gen.wait(timeout=5)
    return p.returncode, err


def run(c, direction, device):
    pcm = "pcm0p" if direction == "playback" else "pcm0c"
    channels = PLAYBACK_CHANNELS if direction == "playback" else CAPTURE_CHANNELS
    rate = int(c.params.get("rate", 48000))
    seconds = float(c.params.get("seconds", 6))
    injections = int(c.params.get("injections", 3))
    capture_path = os.path.join(c.workdir, "xrun_recovery_capture.raw")

    watch = alsa.watch_pcm(c.card, pcm)
    watch.start()
    p, gen = _start(direction, device, rate, seconds, channels, capture_path)

    # Wait for the substream to actually be RUNNING before the first
    # injection: xrun_injection is a safe no-op against a closed or
    # not-yet-running substream, which would otherwise silently turn an
    # injection into nothing rather than a recorded failure.
    deadline = time.monotonic() + min(seconds, 5.0)
    while watch.state != "RUNNING" and time.monotonic() < deadline:
        time.sleep(0.02)
    if watch.state != "RUNNING":
        watch.stop()
        _finish(p, gen, seconds)
        c.fail(f"substream never reached RUNNING (state={watch.state})")
        return

    confirmed = 0
    # Every target is measured from run_start, not from "now" at the top of
    # each iteration -- computing it relative to the current time instead
    # made each wait add to the last one instead of landing at a fixed
    # fraction of the run, so by the last injection the schedule had drifted
    # well past `seconds` and into the substream's post-RUNNING DRAINING
    # state, where snd_pcm_stop_xrun() correctly no-ops. Found the hard way:
    # JT-PCM-009's first hardware run reported 2/3 confirmed, the 3rd landing
    # at ~9s into what was meant to be a 6s clip.
    run_start = time.monotonic()
    for i in range(injections):
        # Spread across the run, clear of the very start/end where a real
        # prepare/drain may already be in flight for reasons that have
        # nothing to do with this case.
        target = run_start + seconds * (i + 1) / (injections + 1)
        while time.monotonic() < target:
            time.sleep(0.02)

        before = watch.xruns
        rc, out, err = priv.xrun_inject(pcm)
        if rc != 0:
            c.fail(f"xrun-inject #{i + 1} failed: "
                   f"{(err or out or '').strip()[:120]}")
            continue

        confirm_deadline = time.monotonic() + INJECT_CONFIRM_S
        while watch.xruns <= before and time.monotonic() < confirm_deadline:
            time.sleep(0.02)
        if watch.xruns <= before:
            c.fail(f"xrun-inject #{i + 1}: xrun counter did not rise within "
                   f"{INJECT_CONFIRM_S}s (state={watch.state})")
        else:
            confirmed += 1
        c.progress(f"  injected xrun {i + 1}/{injections}, "
                   f"xrun_counter now {watch.xruns}")

    rc, err = _finish(p, gen, seconds)
    watch.stop()

    c.metric("injections_requested", injections)
    c.metric("injections_confirmed", confirmed)
    c.metric("xruns", watch.xruns)
    c.metric("avail_max", watch.avail_max)

    tool = "aplay" if direction == "playback" else "arecord"
    if rc != 0:
        c.fail(f"{tool} exited {rc} instead of recovering and finishing: "
               f"{(err or '').strip().splitlines()[-1][:120] if err else ''}")
    if confirmed < injections:
        c.fail(f"only {confirmed}/{injections} injections were confirmed "
               f"to land")


def main():
    c = Case()
    c.require_card()
    c.require_tools("aplay", "arecord", "sox")

    ok, why = priv.available()
    if not ok:
        c.blocked(f"cannot inject xruns: {why}")

    direction = sys.argv[1] if len(sys.argv) > 1 else None
    if direction not in ("playback", "capture"):
        c.blocked(f"unknown direction {direction!r}, want playback or capture")

    device = c.device or alsa.device_name(c.card)
    run(c, direction, device)
    c.done()


if __name__ == "__main__":
    main()
