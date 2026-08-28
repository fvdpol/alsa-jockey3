#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L3: JT-PCM-010, packet/URB coalescing (N) validation sweep.

E2d (re/streaming_overhead_experiments.md) has jockey3_pcm_hw_params() choose
how many Ploytec packets each URB carries per PCM open -- N, the largest
power of two with N * packet_bytes <= period_bytes -- instead of the fixed
N=1 every period size got before. Relaxing .period_bytes_min to the N=1
values (120 B playback / 144 B capture) legalized period sizes that used to
sit below the driver's old fixed floor, and every period size at or above
240 B (playback) / 288 B (capture) now automatically gets a bigger,
coalesced N where it always got N=1 before -- a code path no period size
could reach until now.

JT-PCM-007 (pcm_latency_sweep.py) already runs real, sustained transfers at
doubling period sizes, but it stops at the FIRST clean size: its job is a
single achievable-latency number. That is the wrong shape here. This case's
job is to confirm every N the driver can choose (1, 2, 4, 8) runs clean
under real load, so every candidate below is tested regardless of whether an
earlier one already came back clean.

Candidates are the exact period sizes that land on each N per
jockey3_pcm_set_n()'s formula, a handful of NON-power-of-two multiples in
between to confirm that formula rounds DOWN to the nearest power of two
rather than up or to the nearest (a period that holds 5 packets' worth of
bytes must still get N=4, not N=5 or N=8 -- N=5 is not a legal choice at
all, jockey3_pcm_set_n() never produces one), and one point above the N=8
ceiling as a control that N stays capped rather than growing further:

    ladder= 1 -> N=1    1 x packet   ( 120 B playback /  144 B capture)
    ladder= 2 -> N=2    2 x packets  ( 240 B playback /  288 B capture)
    ladder= 3 -> N=2    3 x packets  ( 360 B playback /  432 B capture, rounds down)
    ladder= 4 -> N=4    4 x packets  ( 480 B playback /  576 B capture)
    ladder= 5 -> N=4    5 x packets  ( 600 B playback /  720 B capture, rounds down)
    ladder= 8 -> N=8    8 x packets  ( 960 B playback / 1152 B capture)
    ladder=12 -> N=8   12 x packets  (1440 B playback / 1728 B capture, rounds down + capped)
    ladder=16 -> N=8   16 x packets  (1920 B playback / 2304 B capture, control)

Real-transfer method (sox piped into aplay for playback, arecord to
/dev/null for capture, exact period/buffer negotiation via
probe_hw_params(), DURATION_S seconds x REPEATS_PER_SIZE repeats) is the
same rigor JT-PCM-007 uses, and its helper functions are duplicated rather
than imported -- cases in this suite are each meant to stand alone (see
pcm_latency_sweep.py's own note on the same choice).

Unlike JT-PCM-007, a candidate that does not run clean here IS a failure:
every size on the ladder sits comfortably above the absolute legal minimum,
so an xrun is a regression signal, not an expected boundary effect. That
includes ladder=1 (N=1) -- unchanged behavior from before E2d, but worth
reconfirming as the baseline every other rung is compared against.

Known limitation: this case does not confirm the driver actually chose the
EXPECTED N at each period size -- that would need the hw_params() dev_dbg
line (see jockey3_pcm_set_n()) read back from the kernel log, which a case
must not do itself (lib/case.py's module docstring: log inspection is the
runner's job). Wiring that up needs a new jockey3-testctl dyndbg verb
(mirroring dyndbg-firmware) plus a runner-side enable/parse step (mirroring
env.enable_firmware_debug()/firmware_from_log()) -- a follow-up, not done
here. What this case DOES confirm directly: real playback/capture survives,
xrun-free, at every period size the driver's N selection actually has to
handle.
"""

import ctypes
import ctypes.util
import os
import subprocess
import sys

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402
from lib import alsa               # noqa: E402

# Fixed for the same reason pcm_latency_sweep.py fixes it: a rate change
# tears down and reprograms the device, which this sweep has no business
# folding in.
RATE = 44100
FORMAT = "S24_3LE"

PLAYBACK_CHANNELS = 4
CAPTURE_CHANNELS = 6
BYTES_PER_SAMPLE = 3
FRAME_BYTES = {"playback": PLAYBACK_CHANNELS * BYTES_PER_SAMPLE,     # 12
               "capture": CAPTURE_CHANNELS * BYTES_PER_SAMPLE}       # 18

# jockey3.c: 10 playback frames / 8 capture frames per Ploytec packet.
# Mirrors jockey3_pcm_set_n()'s packet_bytes; keep in sync by hand, same
# caveat as pcm_limits.py/pcm_latency_sweep.py (no exported hook to read it
# from the running driver).
PACKET_FRAMES = {"playback": 10, "capture": 8}

PERIODS = 2     # smallest legal period count -- see pcm_limits.py PERIODS_MIN

# Sub-packets/URB steps to exercise, in packets. 3, 5 and 12 are deliberately
# NOT powers of two -- jockey3_pcm_set_n() must floor them to the nearest
# legal N (2, 4 and 8 respectively), never round up and never pick a
# non-power-of-two N, which does not exist. 16 is a control point past the
# driver's N=8 ceiling (jockey3_pcm_set_n() clamps to JOCKEY3_PLAYBACK_N/
# JOCKEY3_CAPTURE_N, both 8): it must land on the same N=8 as ladder=8 and
# ladder=12, not grow further.
N_LADDER = (1, 2, 3, 4, 5, 8, 12, 16)
MAX_N = 8


def expected_n(ladder):
    """Largest power of two <= ladder, capped at MAX_N -- exactly mirrors
    jockey3_pcm_set_n()'s clamp_t(u8, ilog2(n), 0, ilog2(max_n))."""
    floored = 1 << (ladder.bit_length() - 1)
    return min(floored, MAX_N)


DURATION_S = 10          # per transfer attempt -- see pcm_latency_sweep.py
REPEATS_PER_SIZE = 3     # consecutive clean transfers required before a size counts

STREAMS = {
    "playback": dict(pcm="pcm0p", channels=PLAYBACK_CHANNELS),
    "capture": dict(pcm="pcm0c", channels=CAPTURE_CHANNELS),
}


# ------------------------------------------------------------ libasound ctypes
#
# Identical in shape to pcm_latency_sweep.py's probe_hw_params(): an exact
# (non-"near") negotiation of period and buffer size, so a size this sweep
# calls "clean" was actually the size the transfer ran at, not something
# alsa-lib silently rounded to.

def _lib():
    if _lib.cache is None:
        path = ctypes.util.find_library("asound") or "libasound.so.2"
        lib = ctypes.CDLL(path)
        lib.snd_pcm_open.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                     ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        lib.snd_pcm_hw_params_malloc.argtypes = [
            ctypes.POINTER(ctypes.c_void_p)]
        lib.snd_pcm_hw_params_set_period_size.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int]
        lib.snd_pcm_hw_params_set_buffer_size.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        lib.snd_pcm_format_value.argtypes = [ctypes.c_char_p]
        lib.snd_strerror.restype = ctypes.c_char_p
        _lib.cache = lib
    return _lib.cache


_lib.cache = None

SND_PCM_STREAM_PLAYBACK = 0
SND_PCM_STREAM_CAPTURE = 1
SND_PCM_ACCESS_RW_INTERLEAVED = 3


def _errstr(lib, rc):
    return lib.snd_strerror(rc).decode(errors="replace")


def probe_hw_params(device, stream, channels, period_frames, buffer_frames):
    """Ask libasound to negotiate these EXACT period/buffer sizes.

    Returns (ok, err). Opens and closes the device purely to negotiate --
    see pcm_limits.py's probe_hw_params() for the full rationale, which
    applies unchanged here.
    """
    lib = _lib()
    pcm = ctypes.c_void_p()
    stream_dir = (SND_PCM_STREAM_PLAYBACK if stream == "playback"
                  else SND_PCM_STREAM_CAPTURE)
    rc = lib.snd_pcm_open(ctypes.byref(pcm), device.encode(), stream_dir, 0)
    if rc < 0:
        return False, f"snd_pcm_open: {_errstr(lib, rc)}"

    params = ctypes.c_void_p()
    rc = lib.snd_pcm_hw_params_malloc(ctypes.byref(params))
    if rc < 0:
        lib.snd_pcm_close(pcm)
        return False, f"hw_params_malloc: {_errstr(lib, rc)}"
    try:
        rc = lib.snd_pcm_hw_params_any(pcm, params)
        if rc < 0:
            return False, f"hw_params_any: {_errstr(lib, rc)}"

        fmt = lib.snd_pcm_format_value(FORMAT.encode())
        setters = (
            ("access", lambda: lib.snd_pcm_hw_params_set_access(
                pcm, params, SND_PCM_ACCESS_RW_INTERLEAVED)),
            ("format", lambda: lib.snd_pcm_hw_params_set_format(
                pcm, params, fmt)),
            ("channels", lambda: lib.snd_pcm_hw_params_set_channels(
                pcm, params, channels)),
            ("rate", lambda: lib.snd_pcm_hw_params_set_rate(
                pcm, params, RATE, 0)),
        )
        for label, setter in setters:
            rc = setter()
            if rc < 0:
                return False, f"set_{label}: {_errstr(lib, rc)}"

        rc = lib.snd_pcm_hw_params_set_period_size(
            pcm, params, ctypes.c_ulong(period_frames), 0)
        if rc < 0:
            return False, f"set_period_size: {_errstr(lib, rc)}"

        rc = lib.snd_pcm_hw_params_set_buffer_size(
            pcm, params, ctypes.c_ulong(buffer_frames))
        if rc < 0:
            return False, f"set_buffer_size: {_errstr(lib, rc)}"

        rc = lib.snd_pcm_hw_params(pcm, params)
        if rc < 0:
            return False, f"hw_params commit: {_errstr(lib, rc)}"
        return True, None
    finally:
        lib.snd_pcm_hw_params_free(params)
        lib.snd_pcm_close(pcm)


# -------------------------------------------------------------- real transfer

def transfer_playback(c, device, period_frames, buffer_frames, seconds):
    """sox generates a real tone, aplay consumes it at the exact size under
    test. Returns (rc, xruns, avail_max, hw_params, err).

    hw_params is what watch_pcm actually observed negotiated while the
    stream was open -- --period-size/--buffer-size on aplay go through
    snd_pcm_hw_params_set_*_near(), the same rounding trap JT-PCM-006's
    notes describe, so the request is not proof of what ran. The caller
    checks this against what was asked for."""
    gen = subprocess.Popen(
        ["sox", "-n", "-r", str(RATE), "-c", str(PLAYBACK_CHANNELS), "-b", "24",
         "-e", "signed-integer", "-t", "raw", "-",
         "synth", str(seconds), "sine", "E3", "gain", "-6"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    watch = alsa.watch_pcm(c.card, "pcm0p")
    watch.start()
    p = subprocess.Popen(
        ["aplay", "-q", "-D", device, "-r", str(RATE), "-c", str(PLAYBACK_CHANNELS),
         "--format", FORMAT, "-t", "raw",
         "--period-size", str(period_frames), "--buffer-size", str(buffer_frames)],
        stdin=gen.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True)
    gen.stdout.close()
    try:
        _out, err = p.communicate(timeout=seconds + 15)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        p.kill()
        gen.kill()
        rc, err = 124, "timed out"
    gen.wait(timeout=5)
    watch.stop()
    return rc, watch.xruns, watch.avail_max, watch.hw_params, err


def transfer_capture(c, device, period_frames, buffer_frames, seconds):
    """arecord to /dev/null at the exact size under test.

    Returns (rc, xruns, avail_max, hw_params, err) -- see transfer_playback()
    for why hw_params (not the requested size) is what gets compared."""
    samples = int(seconds * RATE)
    cmd = ["arecord", "-q", "-D", device, "-f", FORMAT,
           "-c", str(CAPTURE_CHANNELS), "-r", str(RATE),
           "--period-size", str(period_frames), "--buffer-size", str(buffer_frames),
           "--samples", str(samples), "/dev/null"]
    watch = alsa.watch_pcm(c.card, "pcm0c")
    watch.start()
    rc, _out, err = c.run(cmd, timeout=seconds + 15)
    watch.stop()
    return rc, watch.xruns, watch.avail_max, watch.hw_params, err


TRANSFER = {"playback": transfer_playback, "capture": transfer_capture}


# ------------------------------------------------------------------- sweeping

def candidate_periods(stream):
    """(ladder, period_frames) for every N_LADDER step, this stream's
    frame size."""
    frame_bytes = FRAME_BYTES[stream]
    packet_bytes = PACKET_FRAMES[stream] * frame_bytes
    out = []
    for ladder in N_LADDER:
        period_bytes = ladder * packet_bytes
        # Exact -- packet_bytes is itself a whole number of frames, so no
        # rounding is needed the way pcm_latency_sweep.py's ceil-to-frame
        # doubling requires from an arbitrary PERIOD_BYTES_MIN.
        out.append((ladder, period_bytes // frame_bytes))
    return out


def run_one(c, device, stream, ladder, period_frames, seconds, repeats, i, total):
    """Probe once, then transfer `repeats` times. Clean only if every
    repeat ran with zero xruns, exited zero, AND actually negotiated the
    exact size asked for -- see pcm_latency_sweep.py's run_one() for the
    full rationale, which applies unchanged here."""
    info = STREAMS[stream]
    buffer_frames = period_frames * PERIODS
    period_bytes = period_frames * FRAME_BYTES[stream]
    n = expected_n(ladder)

    c.status(f"  [{stream} {i}/{total}] N={n} period={period_bytes}B "
             f"({period_frames}fr) x{PERIODS}...")

    ok, probe_err = probe_hw_params(device, stream, info["channels"],
                                     period_frames, buffer_frames)
    record = {"stream": stream, "ladder": ladder, "expected_n": n,
              "period_frames": period_frames, "period_bytes": period_bytes,
              "buffer_frames": buffer_frames}
    if not ok:
        # Every size on this ladder is at or above the relaxed N=1
        # .period_bytes_min, so a refusal here is a driver fault, not a
        # latency data point -- unlike pcm_latency_sweep.py, there is no
        # "below the legal floor" case on this ladder at all.
        c.fail(f"{stream} N={n} period={period_bytes}B: legal-range config "
               f"refused by hw_params: {probe_err}")
        record["clean"] = False
        record["error"] = probe_err
        return record

    total_xruns = 0
    max_avail_max = 0
    for attempt in range(1, repeats + 1):
        c.status(f"  [{stream} {i}/{total}] N={n} period={period_bytes}B "
                 f"({period_frames}fr) x{PERIODS} -- repeat {attempt}/{repeats}...")
        rc, xruns, avail_max, hw_params, err = TRANSFER[stream](
            c, device, period_frames, buffer_frames, seconds)
        total_xruns += xruns
        max_avail_max = max(max_avail_max, avail_max)

        neg_period = hw_params.get("period_size")
        neg_buffer = hw_params.get("buffer_size")
        if neg_period != period_frames or neg_buffer != buffer_frames:
            # aplay/arecord's --period-size/--buffer-size go through the
            # rounding *_near() setters, not the exact ones probe_hw_params()
            # used -- so a transfer that ran at a different size than what
            # was probed says nothing about the N under test here.
            c.fail(f"{stream} N={n} period={period_bytes}B: transfer "
                   f"negotiated {neg_period}/{neg_buffer} frames, not "
                   f"{period_frames}/{buffer_frames} -- aplay/arecord "
                   "rounded away from the probed size")
            record["clean"] = False
            record["negotiated_period_frames"] = neg_period
            record["negotiated_buffer_frames"] = neg_buffer
            return record

        err_lower = (err or "").lower()
        looks_bad = xruns or rc != 0 or "xrun" in err_lower or "broken pipe" in err_lower
        if looks_bad:
            # Unlike pcm_latency_sweep.py, this IS a failure: every size on
            # this ladder sits well above the absolute legal minimum and is
            # meant to run clean, so an xrun here is the regression this
            # case exists to catch, not an expected boundary effect.
            c.fail(f"{stream} N={n} period={period_bytes}B: not clean on "
                   f"repeat {attempt}/{repeats} (rc={rc}, xruns={xruns}, "
                   f"avail_max={avail_max})")
            record["clean"] = False
            record["xruns"] = total_xruns
            record["avail_max"] = max_avail_max
            return record

    record["clean"] = True
    record["xruns"] = total_xruns
    record["avail_max"] = max_avail_max
    return record


def sweep_stream(c, device, stream, seconds, repeats):
    """Every candidate on the ladder, always -- no early return once one is
    clean. That is the entire difference from pcm_latency_sweep.py's
    sweep_stream()."""
    candidates = candidate_periods(stream)
    results = []
    for i, (ladder, period_frames) in enumerate(candidates, 1):
        record = run_one(c, device, stream, ladder, period_frames, seconds,
                         repeats, i, len(candidates))
        results.append(record)
        verdict = "clean" if record["clean"] else "NOT CLEAN"
        c.progress(f"{stream}: N={record['expected_n']} "
                  f"period={record['period_bytes']}B -- {verdict}")
    return results


def main():
    c = Case()
    c.require_card()
    c.require_tools("aplay", "arecord", "sox")

    device = c.device or alsa.device_name(c.card)
    seconds = c.params.get("duration_s", DURATION_S)
    repeats = c.params.get("repeats_per_size", REPEATS_PER_SIZE)

    for stream in ("playback", "capture"):
        results = sweep_stream(c, device, stream, seconds, repeats)
        c.metric(f"{stream}_results", results)
        clean_count = sum(1 for r in results if r["clean"])
        c.metric(f"{stream}_clean_count", clean_count)
        c.metric(f"{stream}_candidates_tested", len(results))

    c.done()


if __name__ == "__main__":
    main()
