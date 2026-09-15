#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L3: JT-AUDIO-002 -- output presence and channel map, via the device's own
loopback (Master/Headphone out patched back into In1/In2).

WHAT THIS CAN AND CANNOT PROVE
-------------------------------
The round trip is analog: DAC, a cable, an ADC, and whatever gain the bench
has dialed in on the input. That rules out anything that depends on an
absolute level or on exact sample values. It rules out RMS thresholds in
particular -- Frank's point, 2026-08-19: a channel reading -20 dBFS instead
of -6 dBFS says as much about the input trim as it does about the driver, so
a fixed dBFS gate would fail a healthy device or pass a broken one depending
on a knob this case has no way to read. It also rules out proving *data*
integrity (bit-exact sample content survives no better than level does) --
that finding stays open; see docs/test_strategy.md section 12.

What analog crosstalk does NOT wreck is a ratio measured within one capture:
two channels recorded in the same run share whatever gain, drift and cable
loss applied to that run, so a ratio between them cancels it out. That is the
whole design: every verdict here is a same-capture ratio, never an absolute
level.

WHY TONES AND GOERTZEL, NOT A RAMP AND CORRELATION
----------------------------------------------------
The natural instinct for a data-integrity instrument is a deterministic
counter pattern, checked by cross-correlating capture against what was sent.
Two things rule it out here. First, this case cannot prove data integrity
anyway (above), so there is nothing for a counter pattern to buy over a tone.
Second, correlation needs a lag search -- aplay and arecord are two
independent processes with no shared clock, so their relative start offset is
unknown -- and an O(N x lags) search in pure Python, at 96 kHz on armhf-prod,
would make the analyzer slower than the capture it is measuring. A tone per
channel plus a Goertzel bin per tone is O(N) per channel, needs no alignment
at all (a frequency estimate does not care where in the window the tone
started), and is the same handful of arithmetic ops per sample that
`_accumulate_frames()` in pcm.py already runs at this rate on this hardware.

Frequencies reuse JT-AUDIO-001's channel map exactly (E3/A3/D4/G4 for Master
L/R, Headphone L/R) -- the same case a human already runs by ear, so a
mismatch here has a manual cross-check, and the four notes are already known
to be non-harmonically related enough to separate cleanly (measured
2026-08-19: >45 dB bin separation in a noiseless synthetic file at every
supported rate).

THE PATCH MAP IS MACHINE-LOCAL, NOT ASSUMED
---------------------------------------------
Frank has one cable connected, not two. Which output is patched into which
input is bench-specific in the same way a power-switch address is (see
lib/machineconf.py) -- it belongs in ~/.config/jockey3/machine.yaml, never
guessed and never hard-coded:

    audio_loopback:
      patches:
        master: in1        # Master out -> In 1
        # headphone: in2   # add when a second cable is connected

An output with no entry is reported, not failed -- there is nothing wrong
with the device, there is just nothing plugged in to check it with. No
patches configured at all blocks the case outright, since there would be
nothing left to measure.

VERDICT PER PATCHED PAIR, PER RATE
------------------------------------
1. channel_map: for each of the two capture channels in the patched input,
   is ITS OWN target frequency the loudest of the four known tones on that
   channel? This is the channel-map check, and it needs no threshold --
   "which of four known bins is largest" is a ranking, immune to whatever the
   input gain happens to be. A swapped L/R, or a patch into the wrong input,
   shows up as the wrong bin winning.
2. isolation_db: own-tone level against the next loudest of the other three
   known tones, in dB. This is the crosstalk figure -- low isolation on a
   channel that still passed (1) means the tones are close enough that the
   ranking could plausibly flip on a noisier run, so it is worth a look even
   when it is not (yet) a wrong answer.
3. snr_db: own-tone level against that channel's actual measured noise
   floor, in dB. The floor is a real, separate capture with nothing playing
   -- taken once per rate, right before the tones -- rather than inferred
   from an unplayed frequency bin, per Frank's request 2026-08-19: a single
   Goertzel bin only sees noise power at that one frequency, and comparing
   it to a *broadband* RMS floor (what "-80 dBFS noise floor" means, and
   what JT-PCM-005 already reports) would be comparing different things.
   `snr_min_db` defaults to 40 -- comfortably above a healthy ~-80 dBFS
   floor without needing the tones anywhere near full scale, which matters
   because Master/Headphone may have real speakers or cans plugged in for
   the duration of this case (see JT-AUDIO-001/003). A channel whose own
   tone does not clear the floor by that margin reads as inconclusive
   rather than failed -- a bench issue (cable, gain) and a driver defect
   produce the same low-SNR reading, and only a person at the bench can
   tell them apart.
4. clip_fraction: if the input gain is hot enough to clip, harmonics of the
   fundamental spill into neighboring bins and can manufacture a crosstalk
   or channel-map failure that has nothing to do with the driver. Reported
   so a nonsense matrix gets diagnosed rather than filed as a defect.

ONE MORE THING, UNSCORED: THE OBSERVED CHANNEL MAP
------------------------------------------------------
`observed_channel_map()` runs unconditionally on every capture channel, not
just the ones a configured patch covers, and reports which known tone (if
any) is loudest there -- a much looser threshold than the scored checks
above, since it exists to be looked at rather than to pass or fail. A
channel reading a tone nobody expected there names exactly which pair of
RCAs got crossed without anyone reasoning about isolation_db and snr_db by
hand, and it picks up real signal this case was not asked to look for: on
Frank's bench (2026-08-19) a DI box feeds the mic input from the balanced
Master R output, so channels 4/5 correlate with master_r despite carrying
no configured patch -- a wired connection audio_loopback.patches does not
know about, surfaced anyway because the map is unconditional.

Both the tones and the floor are converted from raw Goertzel/RMS power into
the same amplitude-equivalent dBFS scale JT-PCM-005 already uses, because a
Goertzel bin's raw power is not directly comparable across two captures of
different lengths (a coherent tone's bin power grows with the *square* of
the window length; incoherent noise in the same bin does not) -- see
`tone_dbfs()`. rms_dbfs is still recorded per channel too, same as
JT-PCM-005 -- informational only, never gating the verdict.
"""

import math
import os
import subprocess
import sys

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case                          # noqa: E402
from lib import alsa, machineconf                  # noqa: E402
from cases.pcm import (                             # noqa: E402
    PLAYBACK_CHANNELS, CAPTURE_CHANNELS, BYTES_PER_SAMPLE, FORMAT,
    FULL_SCALE, finish_playback, start_capture_async, rms_dbfs_per_channel,
    fmt_dbfs,
)

# Channel map and pitches match JT-AUDIO-001 / play_channel_sweep.sh exactly,
# so a mismatch here has a manual cross-check and the notes are proven
# non-harmonic by construction rather than by hope.
TONES = [("master_l", "E3", 164.81), ("master_r", "A3", 220.00),
         ("headphone_l", "D4", 293.66), ("headphone_r", "G4", 392.00)]

OUTPUT_CHANNELS = {"master": (0, 1), "headphone": (2, 3)}
INPUT_CHANNELS = {"in1": (0, 1), "in2": (2, 3)}

ISOLATION_NOTE_DB = 20.0   # below this, worth a note even if the ranking held
SNR_MIN_DB = 40.0          # below this, the reading is inconclusive, not a fail
# What a healthy input's own noise floor is expected to look like (Frank,
# 2026-08-19). Not a gate -- just what makes a floor reading worth a note
# rather than silently accepted as "whatever it was".
EXPECTED_FLOOR_DBFS = -80.0
FLOOR_NOTE_MARGIN_DB = 20.0
CLIP_THRESHOLD = 0.99      # fraction of full scale considered clipped
CLIP_NOTE_FRACTION = 0.001 # 0.1% of samples clipped is worth a note


def start_playback_tones(device, rate, seconds, channels=PLAYBACK_CHANNELS):
    """One tone per playback channel, per TONES above."""
    args = ["sox", "-n", "-r", str(rate), "-c", str(channels), "-b", "24",
            "-e", "signed-integer", "-t", "raw", "-", "synth", str(seconds)]
    for _name, _note, freq in TONES[:channels]:
        args += ["sine", str(freq)]
    args += ["gain", "-6"]
    gen = subprocess.Popen(args, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    p = subprocess.Popen(
        ["aplay", "-D", device, "-r", str(rate), "-c", str(channels),
         "--format", FORMAT, "-t", "raw"],
        stdin=gen.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True)
    gen.stdout.close()
    return p, gen


def load_channel(path, channel, channels, skip_seconds, keep_seconds, rate):
    """One channel's samples as a list of ints, from a raw S24_3LE capture.

    Trims skip_seconds off the front (sox/aplay/arecord startup is not a
    steady tone) and reads at most keep_seconds after that -- a long, steady
    window is what gives Goertzel its bin separation, but there is no benefit
    to reading the whole file once that window is comfortably met.
    """
    frame = channels * BYTES_PER_SAMPLE
    skip_frames = int(skip_seconds * rate)
    want_frames = int(keep_seconds * rate)
    out = []
    with open(path, "rb") as f:
        f.seek(skip_frames * frame)
        while len(out) < want_frames:
            chunk = f.read(frame * 8192)
            if not chunk:
                break
            for off in range(0, len(chunk) - frame + 1, frame):
                s = chunk[off + channel * BYTES_PER_SAMPLE:
                          off + (channel + 1) * BYTES_PER_SAMPLE]
                out.append(int.from_bytes(s, "little", signed=True))
    return out


def goertzel_power(samples, rate, freq):
    """Signal power at one frequency bin. O(N), no alignment needed."""
    n = len(samples)
    if n == 0:
        return 0.0
    k = int(0.5 + (n * freq) / rate)
    w = 2 * math.pi * k / n
    cosine = math.cos(w)
    coeff = 2 * cosine
    q1 = q2 = 0.0
    for x in samples:
        q0 = coeff * q1 - q2 + x
        q2, q1 = q1, q0
    real = q1 - q2 * cosine
    imag = q2 * math.sin(w)
    return real * real + imag * imag


def tone_dbfs(power, n):
    """A Goertzel bin's power, converted to the same amplitude-equivalent
    dBFS scale as rms_dbfs_per_channel(), so a tone and a separately-measured
    broadband floor can be compared directly.

    For a coherent tone of amplitude A sampled N times, |X(k)| = A*N/2 at its
    own bin -- so amplitude = 2*sqrt(power)/N. That N in the denominator is
    what a raw power-to-power comparison across two different window lengths
    gets wrong: a coherent bin's raw power grows with N**2, while an
    incoherent noise bin's grows only with N, so a longer tone window read
    against a shorter (or longer) floor window would report a made-up SNR
    that only reflects the length mismatch.
    """
    if n <= 0 or power <= 0:
        return float("-inf")
    amplitude = 2 * math.sqrt(power) / n
    rms = amplitude / math.sqrt(2)
    return 20 * math.log10(rms / FULL_SCALE) if rms > 0 else float("-inf")


def record_floor(device, rate, seconds, path):
    """A short capture with nothing playing -- this rate's actual noise
    floor, not a proxy. Blocking: the case does not start the tone pass
    until this returns, so there is no playback bleeding into it.
    """
    duration = max(1, int(round(seconds)))
    subprocess.run(
        ["arecord", "-D", device, "-r", str(rate), "-c", str(CAPTURE_CHANNELS),
         "--format", FORMAT, "-t", "raw", "-d", str(duration), path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=seconds + 30)
    return rms_dbfs_per_channel(path, CAPTURE_CHANNELS)


def clip_fraction(samples):
    if not samples:
        return None
    thresh = CLIP_THRESHOLD * FULL_SCALE
    return sum(1 for v in samples if abs(v) >= thresh) / len(samples)


OBSERVED_MIN_DB = 10.0   # informational floor -- much looser than snr_min_db


def observed_channel_map(c, raw, rate, floor_dbfs, skip_seconds, keep_seconds,
                          min_report_db=OBSERVED_MIN_DB):
    """Diagnostic only, never scored: which known tone (if any) is loudest
    on EVERY physical capture channel, not just the ones audio_loopback's
    patches cover.

    This is what makes a wiring mistake or a driver channel-map defect easy
    to eyeball -- a channel reported against a tone nobody expected there
    says exactly which pair of RCAs got crossed, without needing anyone to
    reason about isolation_db and snr_db by hand. It also surfaces signal
    nobody asked this case to look for: Frank's bench (2026-08-19) has a DI
    box feeding the mic input from the balanced Master R output, so
    channels 4/5 show up correlating with master_r here even though
    audio_loopback.patches has no entry describing that connection at all.
    That is a real, useful cross-check -- an unexpectedly-wired channel
    reading the wrong output, or reading nothing when it should be hearing
    something, points at the same kinds of problems a patched pair does --
    so every channel is reported unconditionally rather than only the ones
    with a configured expectation. The threshold is looser than snr_min_db
    on purpose, since an unconfigured or incidental connection is not
    guaranteed to run as hot as a deliberately patched pair.
    """
    entries = []
    for ch in range(CAPTURE_CHANNELS):
        samples = load_channel(raw, ch, CAPTURE_CHANNELS,
                                skip_seconds, keep_seconds, rate)
        n = len(samples)
        levels = {name: tone_dbfs(goertzel_power(samples, rate, f), n)
                  for name, _note, f in TONES}
        loudest = max(levels, key=levels.get)
        floor = floor_dbfs[ch] if floor_dbfs else None
        snr = (levels[loudest] - floor) if floor is not None else float("nan")
        if math.isfinite(snr) and snr >= min_report_db:
            label = f"{loudest}(+{snr:.0f}dB)"
        else:
            label = "floor"
        c.metric(f"observed_ch{ch}_{rate}", label)
        entries.append(f"ch{ch}={label}")
    c.progress(f"  {rate} Hz: observed map  " + "  ".join(entries))


def analyze_pair(c, raw, rate, input_name, in_channels, out_name,
                  out_channels, skip_seconds, keep_seconds, floor_dbfs,
                  snr_min_db):
    """One patched pair (e.g. master -> in1) at one rate. Records metrics,
    fails on a real channel-map defect, notes the rest.

    floor_dbfs is this rate's actual measured noise floor per capture
    channel, from record_floor() -- a real capture with nothing playing,
    not a proxy bin within this tone capture.
    """
    tone_by_out_channel = {i: TONES[i] for i in out_channels}
    ok = True
    for cap_ch, out_ch in zip(in_channels, out_channels):
        samples = load_channel(raw, cap_ch, CAPTURE_CHANNELS,
                                skip_seconds, keep_seconds, rate)
        n = len(samples)
        levels = {name: tone_dbfs(goertzel_power(samples, rate, f), n)
                  for name, _note, f in TONES}

        own_name = tone_by_out_channel[out_ch][0]
        own_db = levels[own_name]
        others = sorted(v for k, v in levels.items() if k != own_name)
        isolation_db = own_db - others[-1]

        loudest = max(levels, key=levels.get)
        floor = floor_dbfs[cap_ch] if floor_dbfs else None
        # Gated on whichever tone is loudest, not specifically the expected
        # one: a full channel-map swap puts a strong, clearly-present tone
        # on this channel too, just the wrong one. Gating on own_db here
        # would read that as "nothing present" and report it as an
        # inconclusive dead channel instead of the channel-map defect it is.
        snr_db = (levels[loudest] - floor) if floor is not None else float("nan")
        conclusive = math.isfinite(snr_db) and snr_db >= snr_min_db
        clipf = clip_fraction(samples)

        prefix = f"{out_name}/{input_name}_ch{cap_ch}_{rate}"
        c.metric(f"isolation_db_{prefix}",
                 round(isolation_db, 1) if math.isfinite(isolation_db) else None)
        c.metric(f"snr_db_{prefix}",
                 round(snr_db, 1) if math.isfinite(snr_db) else None)
        c.metric(f"floor_dbfs_{prefix}", floor)
        c.metric(f"clip_fraction_{prefix}",
                 round(clipf, 5) if clipf is not None else None)

        if floor is not None and floor > EXPECTED_FLOOR_DBFS + FLOOR_NOTE_MARGIN_DB:
            c.note(f"{prefix}: noise floor {floor:.1f} dBFS is well above "
                   f"the ~{EXPECTED_FLOOR_DBFS:.0f} dBFS expected on a "
                   f"healthy input -- possible hum, ground loop, or wrong "
                   f"input selected (line vs phono)")

        if not conclusive:
            c.note(f"{prefix}: SNR {snr_db} dB against a {floor} dBFS floor, "
                   f"below {snr_min_db} -- inconclusive (check cable/gain), "
                   f"not scored")
            continue
        if clipf and clipf > CLIP_NOTE_FRACTION:
            c.note(f"{prefix}: {clipf * 100:.2f}% of samples clipped -- "
                   f"turn down input gain, matrix may be unreliable")

        c.metric(f"channel_map_ok_{prefix}", loudest == own_name)
        if loudest != own_name:
            ok = False
            c.fail(f"{prefix}: expected {own_name} to be loudest, "
                   f"got {loudest} (own={own_db:.1f} dBFS, "
                   f"{loudest}={levels[loudest]:.1f} dBFS)")
        elif isolation_db < ISOLATION_NOTE_DB:
            c.note(f"{prefix}: only {isolation_db:.1f} dB isolation from "
                   f"the next-loudest tone (threshold for a note: "
                   f"{ISOLATION_NOTE_DB})")
    return ok


def run(c, device):
    c.require_tools("aplay", "arecord", "sox")
    rates = c.params.get("rates", [44100, 48000, 88200, 96000])
    seconds = float(c.params.get("seconds", 3))
    skip_seconds = float(c.params.get("skip_seconds", 0.3))
    keep_seconds = max(1.0, seconds - skip_seconds - 0.2)
    floor_seconds = float(c.params.get("floor_seconds", 1.5))
    snr_min_db = float(c.params.get("snr_min_db", SNR_MIN_DB))

    patches = machineconf.section("audio_loopback").get("patches") or {}
    patches = {k: v for k, v in patches.items()
               if k in OUTPUT_CHANNELS and v in INPUT_CHANNELS}
    if not patches:
        c.blocked("no audio_loopback.patches configured in "
                  "~/.config/jockey3/machine.yaml -- nothing to measure. "
                  "See the docstring in cases/audio_loopback.py.")
        return

    unpatched = [o for o in OUTPUT_CHANNELS if o not in patches]
    if unpatched:
        c.note("no cable configured for: " + ", ".join(unpatched)
               + " -- reported as untested, not failed")

    total_xruns_p = total_xruns_c = 0
    unobserved = []
    for rate in rates:
        c.status(f"  {rate} Hz: measuring noise floor...")
        floor_raw = os.path.join(c.workdir, f"loopback_floor_{rate}.raw")
        floor_dbfs = record_floor(device, rate, floor_seconds, floor_raw)
        c.metric(f"floor_dbfs_{rate}", floor_dbfs)
        c.progress(f"  {rate} Hz: floor  {fmt_dbfs(floor_dbfs)} dBFS")

        c.status(f"  {rate} Hz: {seconds}s tones through "
                  f"{', '.join(sorted(patches))}...")
        raw = os.path.join(c.workdir, f"loopback_capture_{rate}.raw")

        watch_p = alsa.watch_pcm(c.card, "pcm0p")
        watch_c = alsa.watch_pcm(c.card, "pcm0c")
        watch_p.start()
        watch_c.start()

        arec = start_capture_async(device, rate, seconds, raw)
        p, gen = start_playback_tones(device, rate, seconds)

        try:
            _out, cap_err = arec.communicate(timeout=seconds + 30)
            cap_rc = arec.returncode
        except subprocess.TimeoutExpired:
            arec.kill()
            cap_rc, cap_err = 124, "arecord did not finish"
        play_rc, play_err = finish_playback(p, gen, seconds)

        watch_p.stop()
        watch_c.stop()
        total_xruns_p += watch_p.xruns
        total_xruns_c += watch_c.xruns
        if not watch_p.saw_open or not watch_c.saw_open:
            unobserved.append(str(rate))

        if play_rc != 0:
            c.fail(f"{rate} Hz: aplay exited {play_rc}: "
                   f"{(play_err or '').strip().splitlines()[-1][:120] if play_err else ''}")
            continue
        if watch_p.xruns:
            # Recorded in xruns_playback, not failed -- a headroom fact at
            # this rate/host, not a functional defect (pcm_limits.py's
            # reasoning applies here too).
            c.progress(f"  {rate} Hz: {watch_p.xruns} playback xrun(s)")
        if cap_rc != 0:
            c.fail(f"{rate} Hz: arecord exited {cap_rc}: "
                   f"{(cap_err or '').strip()[:120]}")
            continue
        if watch_c.xruns:
            c.progress(f"  {rate} Hz: {watch_c.xruns} capture xrun(s)")

        levels = rms_dbfs_per_channel(raw, CAPTURE_CHANNELS)
        c.metric(f"rms_dbfs_{rate}", levels)
        c.progress(f"  {rate} Hz: capture RMS  {fmt_dbfs(levels)} dBFS")

        observed_channel_map(c, raw, rate, floor_dbfs, skip_seconds, keep_seconds)

        for out_name, in_name in patches.items():
            analyze_pair(c, raw, rate, in_name, INPUT_CHANNELS[in_name],
                         out_name, OUTPUT_CHANNELS[out_name],
                         skip_seconds, keep_seconds, floor_dbfs, snr_min_db)

    c.metric("xruns_playback", total_xruns_p)
    c.metric("xruns_capture", total_xruns_c)
    c.metric("rates_tested", len(rates))
    c.metric("patches_tested", sorted(patches.items()))
    if unobserved:
        c.note("a stream was never observed open at: " + ", ".join(unobserved)
               + " -- xrun figures for those rates mean nothing")


def main():
    c = Case()
    c.require_card()
    device = c.device or alsa.device_name(c.card)
    run(c, device)
    c.done()


if __name__ == "__main__":
    main()
