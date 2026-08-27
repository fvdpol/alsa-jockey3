#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L3: switch sample rates in a loop.

The most important case in the catalog, and the one with the least obvious
criterion.

Capture stall after a rate change is EXPECTED on a healthy build. The driver
logs it deliberately, on every occurrence, so that stall frequency can be
tracked. A test that failed on the presence of a warning would fail every run
on a perfectly good driver.

So what is checked is whether each rate change ends with a working stream:

    stalled and recovered  -> pass, and counted
    stalled and stuck      -> fail

BOTH DIRECTIONS, AND WHY THAT MATTERS
-------------------------------------
This case used to play only. That made it a playback rate-change test wearing
a general name, and it hid the thing it was built to find. With no capture
stream open, jockey3_pcm_hw_params() takes its *deferred* branch on a capture
stall -- it logs and moves on rather than resetting -- so a run could report 19
capture stalls in 20 changes and still pass, having never once checked whether
capture produced audio afterwards.

So each change now plays and records simultaneously, and the recording is
checked the way JT-AUDIO-005 checks it: a running converter is never bit-exact
silent, so an all-zero capture is an engine that did not restart.

THE THREE RECOVERY BRANCHES, AND CHOOSING BETWEEN THEM DELIBERATELY
-------------------------------------------------------------------
Which code path a rate change takes is decided by one thing: whether a capture
substream was open at the moment jockey3_pcm_hw_params() ran. That in turn is
decided by which of the two processes reached hw_params first -- and until this
case grew `rate_change_stream`, that was a race between aplay and arecord that
nobody was controlling and nobody was recording. A run nominally testing the
reset path could spend an unknown fraction of its changes on the deferred one.

    rate_change_stream: capture    capture opens first, so it performs the
                        change with capture_open=1. hw_params sees a dead
                        capture stream and calls jockey3_recover_urb_stream()
                        on the spot -- URB restart first, full reset only if
                        that fails.

    rate_change_stream: playback   playback opens first and changes the rate
                        with capture_open=0, so hw_params defers on capture;
                        the capture open that follows lands in
                        jockey3_pcm_prepare() and calls the same
                        jockey3_recover_urb_stream() from there instead.

    capture: false                 nothing ever opens capture, so the deferred
                        stall is never picked up at all.

The last one is not a third sample of the same quantity, and this matters for
reading any comparison between the arms. Once capture has stalled with nothing
to recover it, the stream stays dead for the rest of the run, and hw_params
re-logs "Capture URB has stalled." at every subsequent change because the
stream never came back. A playback-only run reporting a stall on 19 of 20
changes is reporting one stall re-detected nineteen times, not nineteen
independent events. Per-change *incidence* can only be measured on an arm where
each change starts from a capture stream something restored.

WHAT SEPARATES A DURATION EFFECT FROM A RECOVERY-TIME EFFECT
------------------------------------------------------------
`gap_seconds` idles between changes with nothing open. It exists so that "the
device needs time between changes" can be tested without moving
`seconds_per_rate`, which would change the length of the measurement at the
same time and confound the two. Before Stage 3 of the recovery-unification
cleanup there was a concrete mechanism for it: the reset that hw_params queued
via usb_queue_reset_device() was NOT waited for there -- only
jockey3_recover_capture_stream() waited -- so at short durations change N+1
could begin while change N's reset was still in flight. Since Stage 3,
jockey3_recover_urb_stream() runs (and, when it escalates, waits for
jockey3_wait_for_reset_completion()) from hw_params() directly, so that
specific confound is gone: hw_params() now returns only once any recovery it
triggered has settled. `gap_seconds` is kept as a parameter regardless -- idle
time between changes may still matter for reasons unrelated to an in-flight
reset, and the parameter is cheap to keep available for that.

MEASURING THE RATE FROM THE DEVICE'S CLOCK, NOT FROM A STOPWATCH
-----------------------------------------------------------------
The rate is measured from hw_ptr -- the frames the hardware itself has moved --
sampled from /proc/asound while the stream runs, and taken in steady state.
See alsa.pointer_rate() for the mechanics.

Timing a whole aplay or arecord invocation, which is what this case did before,
gives rate / (1 + startup/duration): a fixed offset that reads 10-17% low at
four seconds even on a perfect device, and whose only remedy is a longer run.
The pointer has start-up, device open and every buffer in the path outside the
window instead of inside it, needs about 1.5 s of stream to work, and lands
near 0.2% against a 5% target. Buffer size is irrelevant to it -- a buffer
shifts latency, not rate -- so nothing here tunes one.

A pointer that stops and resumes is a STALL, not a slow clock, and is reported
as one. Averaging a 1.5 s plateau into a 4 s window reads 37% slow, which would
file the fault this driver has under the fault it does not.

Stall counts and reset-completion delays come from the kernel log and are
classified by the runner, not here. This case's job is to provoke the
transitions, attribute each driver message to the change that caused it, and
confirm audio still flows afterwards.

The rate list is deliberately not sorted: the failure is worst on a downward
switch, so a sorted sweep would systematically test the easy direction.
"""

import os
import re
import subprocess
import sys
import time
from typing import NamedTuple, Optional

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402
from lib import alsa, kmsg         # noqa: E402

CHANNELS = 4                       # playback: Master L/R + Headphone L/R
CAPTURE_CHANNELS = 6               # fixed by the driver: min == max
FORMAT = "S24_3LE"
BYTES_PER_SAMPLE = 3

# States a substream reaches only after hw_params has been applied. Used to
# decide that a process has passed the point where the rate change happens,
# which is what makes rate_change_stream deterministic rather than hopeful.
CONFIGURED = {"SETUP", "PREPARED", "RUNNING", "DRAINING", "XRUN"}


def nonzero_fraction(path):
    """(fraction, frames) for a raw capture. See JT-AUDIO-005.

    Frames are returned alongside the fraction because "no samples at all" and
    "samples that are all zero" are different faults and must not be collapsed
    -- see capture_outcome().
    """
    frame = CAPTURE_CHANNELS * BYTES_PER_SAMPLE
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
    return ((live / total) if total else None), total


def capture_outcome(rc, err, path, floor):
    """Classify one capture attempt. Returns (verdict, fraction, frames, detail).

    Four outcomes, deliberately kept apart:

      error   arecord itself failed -- an open or an ioctl was refused, which
              is what a stalled stream tends to produce.
      nodata  arecord succeeded but delivered no frames. The stream was never
              fed. This is NOT silence and must not be reported as such.
      silent  frames arrived and every one is bit-exact zero, so the converter
              is not running even though the transport is.
      live    a noise floor is present, which is what a running ADC always has.

    The distinction is not pedantic. On this driver a rate change can leave the
    capture URB stream stalled while playback continues perfectly -- audible on
    the speakers -- so a verdict claiming "the device never started its audio
    engine" would be plainly wrong, and would point an investigation at the
    converter when the transport is what stopped.
    """
    if rc != 0:
        last = (err or "").strip().splitlines()
        return "error", None, 0, f"arecord exit {rc}: {last[-1][:100] if last else ''}"
    frac, frames = nonzero_fraction(path)
    if not frames:
        return "nodata", None, 0, "arecord returned 0 frames"
    if frac is None or frac < floor:
        return "silent", frac, frames, f"{frac:.4%} non-zero of {frames} frames"
    return "live", frac, frames, f"{frac:.2%} non-zero of {frames} frames"


def start_capture(device, rate, seconds, path):
    """Begin recording, to run concurrently with playback.

    arecord -d takes whole seconds, and rounds to zero below half a second --
    where it means "no limit", so the recording would run until the case timed
    out rather than for the fraction of a second that was asked for. Clamped,
    because --param makes sub-second durations reachable from the command line.
    """
    duration = max(1, int(round(seconds)))
    return subprocess.Popen(
        ["arecord", "-D", device, "-r", str(rate), "-c",
         str(CAPTURE_CHANNELS), "--format", FORMAT, "-t", "raw",
         "-d", str(duration), path],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def start_playback(device, rate, seconds):
    """Begin playing a tone. Returns (aplay, sox) so the caller can wait."""
    gen = subprocess.Popen(
        ["sox", "-n", "-r", str(rate), "-c", str(CHANNELS), "-b", "24",
         "-e", "signed-integer", "-t", "raw", "-",
         "synth", str(seconds), "sine", "E3", "gain", "-6"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    p = subprocess.Popen(
        ["aplay", "-D", device, "-r", str(rate), "-c", str(CHANNELS),
         "--format", FORMAT, "-t", "raw"],
        stdin=gen.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True)
    gen.stdout.close()
    return p, gen


def finish_playback(p, gen, seconds, t0):
    try:
        _out, err = p.communicate(timeout=seconds + 30)
    except subprocess.TimeoutExpired:
        p.kill()
        gen.kill()
        return 124, "timed out", None
    elapsed = time.time() - t0
    gen.wait(timeout=5)
    return p.returncode, err, elapsed


def wait_configured(watcher, timeout=5.0):
    """Block until a substream has passed hw_params, or the timeout expires.

    This is what makes `rate_change_stream` a setting rather than a wish. The
    rate change happens inside the first hw_params of the pair; the second one
    finds the rate already set and returns without touching the device. So the
    only way to say which direction performs the change is to let one process
    get all the way through hw_params before starting the other.

    /proc's status file is readable only while the substream is open and
    reports OPEN until hw_params has been applied, so a configured state is
    exactly the signal wanted. It is read from the watcher already polling the
    substream rather than by opening a second reader on it: snd_pcm_status64()
    zeroes avail_max and overrange on every read, so two readers steal each
    other's counters. The cost is that this resolves at the watcher's poll
    interval, which is 20 ms against a start-up measured in hundreds.

    Returns the seconds waited, or None on timeout -- which the caller records
    rather than treats as fatal: a stream that could not be configured is a
    finding for the case to report through its normal channels, not a reason to
    abandon the sweep.
    """
    deadline = time.time() + timeout
    t0 = time.time()
    while time.time() < deadline:
        if watcher.state in CONFIGURED:
            return time.time() - t0
        time.sleep(watcher.interval / 2)
    return None


class CaptureRun(NamedTuple):
    """One capture attempt, kept as a record rather than a bare tuple.

    It was a 5-tuple, then a 7-tuple, and one of the three places that unpacked
    it was missed -- the case ran the whole 31-second sweep and then died in the
    metric loop with "too many values to unpack". Named fields make that a
    typo caught at the point of use instead of after the hardware time is spent.
    """
    n: int
    rate: int
    fraction: Optional[float]
    frames: int
    rate_ratio: Optional[float]
    effective_hz: Optional[float]
    frames_ratio: Optional[float]


class Steady(NamedTuple):
    """One direction's steady-state pointer measurement for one change.

    NOTE there are TWO of these per change on a full-duplex run, one per
    direction, so anything aggregated over them has a denominator of
    2 x changes -- not changes. resets_per_change_pct and its companions are
    counted from the kernel log against `changes` and do not come from here;
    keep it that way, or the two denominators will be mixed silently.
    """
    n: int
    rate: int
    direction: str             # 'playback' or 'capture'
    pr: object                 # alsa.PointerRate
    xruns: int
    hw_params: dict


class Change(NamedTuple):
    """Everything known about one rate change, in one place.

    The per-change record is the point of the rewrite. "Nine stalls happened
    somewhere in the run" cannot answer whether the fault follows the direction
    of the switch, the pair of rates, or simply the first change after probe --
    and those are three different bugs.
    """
    n: int
    loop: int
    rate: int
    prev: Optional[int]        # None on change 1: see main() for why
    direction: str             # 'up', 'down' or 'first'
    step: Optional[float]      # |prev/rate - 1|, the size of the transition
    family_cross: Optional[bool]   # 44k1 <-> 48k, i.e. a clock-family change
    changer: str               # which stream performed the change
    mark_start: object
    mark_end: object


def clock_family(rate):
    """'44k1' or '48k'. The two families are different clock sources.

    A 44100 -> 88200 step doubles within one family; 96000 -> 44100 crosses.
    Whether that distinction matters here has never been looked at, which is
    reason enough to record it.
    """
    return "44k1" if rate % 11025 == 0 else "48k"


# Counted here as well as by the runner's classifier. The classifier gives the
# run totals after the fact; these give the operator the number while the run is
# still on screen, and attribute each stall to the change that caused it -- which
# a run-level total cannot do.
#
# "Capture URB has stalled." is emitted by jockey3_wait_urb_stream_started()
# from several call sites, with one identical string: the post-rate-change
# checks in hw_params(), the confirmation in prepare(), and twice inside
# jockey3_recover_urb_stream() (after the lightweight URB restart, and after
# the full reset) -- and that function itself now runs from both hw_params()
# and prepare(). Counting occurrences of the string therefore does not count
# stalls -- one change can legitimately contain several, and a run that
# escalated once would read as more than one stall.
#
# What disambiguates them is the line that FOLLOWS: each caller logs its own
# context immediately afterwards. So the stall lines are classified by looking
# ahead to the next context line rather than by the string itself, which is
# what classify_events() does.
STALL_LINE = re.compile(r"(Playback|Capture) URB has stalled")

# jockey3_recover_urb_stream() is shared by both directions and reached from
# four places, distinguished only by the "context" string each caller passes
# it: "rate change" (hw_params(), for EITHER direction), "opening a capture
# stream" (prepare(), capture branch), "preparing a playback stream"
# (prepare(), playback branch), "watchdog" (jockey3_watchdog_check(), for
# EITHER direction -- not distinguished into its own event names below since
# it is not attributable to any one rate change; see watchdog_* below
# instead). Each of the first three contexts gets its own light-retry/
# escalate/give-up event names below so the capture-open path (the one this
# milestone has always been judged on) keeps its own counters rather than
# being merged into an aggregate that could hide a change in it.
CONTEXT = [
    ("prepare_capture",
     re.compile(r"Capture stream stalled \(opening a capture stream\); "
                r"restarting URBs to recover")),
    ("prepare_playback",
     re.compile(r"Playback stream stalled \(preparing a playback stream\); "
                r"restarting URBs to recover")),
    ("hw_params_light_retry",
     re.compile(r"(?:Playback|Capture) stream stalled \(rate change\); "
                r"restarting URBs to recover")),
    # Every name below ends in the SAME call: usb_queue_reset_device(). There
    # is no such thing here as a lesser reset and a fuller one -- what differs
    # is only which light retry failed first, i.e. which context. An earlier
    # version of this file called one of them "escalated to full reset" and
    # printed it next to a plain count of resets, which read as though nine
    # resets had happened and none of them had been full.
    #
    #   reset_on_rate_change      hw_params()'s own light retry (context
    #                             "rate change") did not bring the stream back
    #   reset_after_urb_restart   prepare()'s capture-open light retry
    #                             (context "opening a capture stream") did not
    #   reset_after_playback_prepare
    #                             prepare()'s playback light retry (context
    #                             "preparing a playback stream") did not
    #   reset_on_watchdog        jockey3_watchdog_work()'s own light retry
    #                             (context "watchdog") did not -- not tied
    #                             to any PCM call, so it can happen inside a
    #                             change window without hw_params() or
    #                             prepare() having done anything at all
    ("reset_on_rate_change",
     re.compile(r"(?:Playback|Capture) stream still stalled after URB restart; "
                r"queuing full USB reset \(rate change\)")),
    ("reset_after_urb_restart",
     re.compile(r"Capture stream still stalled after URB restart; queuing "
                r"full USB reset \(opening a capture stream\)")),
    ("reset_after_playback_prepare",
     re.compile(r"Playback stream still stalled after URB restart; queuing "
                r"full USB reset \(preparing a playback stream\)")),
    # jockey3_watchdog_work()'s own light-retry-then-escalate call
    # (jockey3.c:1744, context "watchdog") -- independent of a rate change
    # or a PCM open, so it can land inside a change window that never
    # touched hw_params()'s or prepare()'s own recovery path at all. Added
    # 2026-08-26 after a full device reset in this context was found
    # silently missing from resets_total_device: every one of
    # JT-RATE-003's 20260826T005005Z-functional 176 "self-recovered
    # stalls" was actually this event, confirmed 176 == the run's raw
    # `reset high-speed USB device` count. See re/rate_change_stall.md.
    ("reset_on_watchdog",
     re.compile(r"(?:Playback|Capture) stream still stalled after URB restart; "
                r"queuing full USB reset \(watchdog\)")),
    # The budget-exhausted give-up: chip-wide, so it can fire from any of the
    # three contexts above without a light retry having even been tried on
    # THIS call -- jockey3_recovery_budget_take() may already be spent from a
    # different direction or a different call site entirely.
    ("recovery_budget_exhausted",
     re.compile(r"(?:Playback|Capture) stream still stalled after URB "
                r"restart; recovery budget exhausted, not resetting")),
    ("stalled_after_full_reset",
     re.compile(r"(?:Playback|Capture) stream still stalled after full USB "
                r"reset; hardware may need power-cycling")),
    ("reset_timeout",
     re.compile(r"Timeout waiting for reset completion")),
    # jockey3_watchdog_check() has tagged its own onset line "startup" or
    # "steady-state" since 2026-08-26 (jockey3.c's JOCKEY3_WATCHDOG_STARTUP_GRACE_MS
    # work) -- mirroring which threshold caught it, not which recovery path
    # ran. "startup" means the grace period itself was exceeded before the
    # first completion after a restart; only "steady-state" is a stall in
    # the sense the case's docstring means (a stream that WAS completing URBs
    # normally and then stopped). The untagged fallback below keeps this
    # readable against a dmesg predating the tag.
    ("watchdog_onset_startup",
     re.compile(r"(?:Playback|Capture) URB stream stalled: no completion for "
                r"(\d+) ms \(\d+ URBs in flight, substream \w+, startup\)")),
    ("watchdog_onset_steady_state",
     re.compile(r"(?:Playback|Capture) URB stream stalled: no completion for "
                r"(\d+) ms \(\d+ URBs in flight, substream \w+, steady-state\)")),
    ("watchdog_onset",
     re.compile(r"(?:Playback|Capture) URB stream stalled: no completion for "
                r"(\d+) ms")),
    ("watchdog_recovered",
     re.compile(r"(?:Playback|Capture) URB stream recovered after (\d+) ms")),
    ("watchdog_restarted",
     re.compile(r"(?:Playback|Capture) URB stream restarted after stalling for "
                r"(\d+) ms")),
]

# Where a "Capture URB has stalled." belongs, given the context line that comes
# next. Anything else -- including no further context at all -- means the check
# in hw_params(), which is the one that measures the rate change itself, and
# which announces itself only through dev_dbg when it defers.
STALL_SITE = {
    "prepare_capture": "capture_stall_on_open",
    "reset_after_urb_restart": "capture_stall_after_urb_restart",
    "stalled_after_full_reset": "capture_stall_after_reset",
}


def resolve_pending(out, pending_cap, pending_pb, context):
    """Append the stall lines waiting on `context`, named by their call site.

    Playback stalls are never renamed: only one caller logs each of them in a
    way worth distinguishing, and letting a capture context line claim one
    would lose it entirely.
    """
    for _ in range(pending_pb):
        out.append(("playback_stall", None))
    for i in range(pending_cap):
        last = i == pending_cap - 1
        out.append((STALL_SITE.get(context, "capture_stall_hw_params")
                    if last else "capture_stall_hw_params", None))


def classify_events(lines):
    """Turn a slice of the kernel log into named events, in order.

    Returns a list of (name, match) with the identical stall strings already
    resolved to their call site. Everything downstream counts these names, so
    no counter anywhere has to know that four call sites share one message.

    Only the LAST unresolved capture stall takes the context line's meaning.
    That rule is what the deferred branch needs: hw_params logs its stall and
    then says nothing further (its "deferring recovery" line is dev_dbg), so
    the next capture open logs a second stall and the pair arrives back to back
    ahead of a single "stalled when opening capture stream". Giving the context
    to every pending stall charged both to the open and lost the one that
    measured the rate change -- which is the number the whole case is for.
    Nothing in the driver logs two stalls in a row from one call site, so an
    earlier pending stall can only be the unannounced one from hw_params.
    """
    out = []
    pending_cap = 0
    pending_pb = 0
    for line in lines:
        for name, rx in CONTEXT:
            m = rx.search(line)
            if not m:
                continue
            resolve_pending(out, pending_cap, pending_pb, name)
            pending_cap = pending_pb = 0
            out.append((name, m))
            break
        else:
            m = STALL_LINE.search(line)
            if m:
                if m.group(1) == "Playback":
                    pending_pb += 1
                else:
                    pending_cap += 1
    resolve_pending(out, pending_cap, pending_pb, None)
    return out


def window_lines(lines, marks):
    """Slice the log into the windows the markers delimit.

    Each change writes a marker before it starts and another after both streams
    have closed, so the log splits into alternating 'change' and 'gap' windows.
    The end marker is what makes the gap a window of its own: the reset that
    hw_params queues is not waited for, so its completion routinely lands after
    the streams are shut. Ending each window at the *next start* marker, as this
    did before, charged those lines to the following change.

    Markers that failed to write are skipped; the windows either side simply
    merge, which is noisier but not wrong.
    """
    positions = []
    for kind, n, mark in marks:
        if not mark.written:
            continue
        idx = None
        for i, line in enumerate(lines):
            if mark.token in line:
                idx = i
        if idx is not None:
            positions.append((idx, kind, n))
    positions.sort()
    out = []
    for pos, (idx, kind, n) in enumerate(positions):
        end = positions[pos + 1][0] if pos + 1 < len(positions) else len(lines)
        out.append((kind, n, lines[idx + 1:end]))
    return out


def count_events(events):
    counts = {}
    for name, _m in events:
        counts[name] = counts.get(name, 0) + 1
    return counts


def branch_of(counts):
    """Which recovery path this change actually took.

    Recorded rather than assumed. Even with rate_change_stream set, a slow or
    failing open can put a change on the other branch, and a comparison between
    arms is worthless if the arm silently changed underneath it.

    The order of the tests is a precedence, not a partition. Since Stage 3,
    hw_params()'s own call to jockey3_recover_urb_stream() waits for any reset
    it triggers to complete before hw_params() returns, which closes the
    specific race this docstring used to describe (a prepare() landing while
    hw_params()'s fire-and-forget reset was still in flight). A change can
    still legitimately carry more than one recovery event -- e.g. a fresh,
    independent stall surfacing at the prepare() that follows -- so
    branch_reset is still NOT guaranteed to be a count of clean single resets;
    read reset_after_urb_restart_total, reset_after_playback_prepare_total and
    prepare_capture_total beside it, and the per-change metrics for any
    individual change.
    """
    if counts.get("reset_on_rate_change") or counts.get("reset_on_watchdog"):
        return "reset"
    if counts.get("prepare_capture"):
        return "recovered_on_open"
    if counts.get("capture_stall_hw_params"):
        return "deferred"
    return "clean"


def sweep_blind_spots(order, tol):
    """Transitions where staying at the previous rate would go unnoticed.

    The elapsed-time check can only see a rate that is wrong by more than the
    tolerance, so the sweep has to be built to make every transition a large
    one. 44100 against 48000 is 8.1% -- a device that ignored the change and
    stayed put would sail through a 20% tolerance.

    The default interleave already satisfies this (worst case 0.500 against a
    0.20 tolerance) because alternating from the ends of a sorted list pairs
    the extremes. A two-rate list does not: rates [44100, 48000] leaves 8.1%
    as the only transition, and the check is then blind for the whole run.

    Returned as pairs rather than raised, so the caller can say which
    configuration is at fault.
    """
    bad = []
    for i, new in enumerate(order):
        prev = order[i - 1]          # cyclic: the sweep repeats each loop
        if prev == new:
            continue
        if abs(prev / new - 1.0) <= tol:
            bad.append((prev, new, round(abs(prev / new - 1.0), 3)))
    return bad


def rate_ratio(observed, expected):
    """How far an observed quantity is from what the nominal rate predicts.

    Returned as a ratio rather than a verdict, because it is worth recording
    even when it passes: drift over time is visible in the metric and not in a
    pass/fail.
    """
    if not expected or observed is None:
        return None
    return round(observed / expected, 3)


def interleave(rates):
    """Order the sweep so every step is a real change, including downward.

    Alternating from the ends -- highest, lowest, second highest, ... -- makes
    every transition a large one in an alternating direction, which is the
    shape that provokes the fault.

    For the default four rates this yields [96000, 44100, 88200, 48000]: two up
    steps and two down per loop, so it is balanced for the DIRECTION question
    and gives one sample of each distinct transition per loop.

    It cannot answer the clock-family question, and no reordering of these four
    rates can. The transitions are 96000->44100, 44100->88200, 88200->48000,
    48000->96000: both downward steps cross between the 44k1 and 48k families
    and both upward steps stay within one. Direction and family crossing are
    perfectly correlated, so a result that follows one follows the other and
    the two cannot be told apart. (An earlier version of this docstring claimed
    the opposite.)

    Separating them needs a sweep this function does not produce -- a downward
    step within a family, or an upward step across one. Use sweep_order:
    as-given for that, e.g. rates [96000, 48000, 96000, 44100], which gives
    96000->48000 down-within, 48000->96000 up-within, 96000->44100 down-cross
    and 44100->96000 up-cross.
    """
    rs = sorted(rates, reverse=True)
    out, lo, hi = [], 0, len(rs) - 1
    while lo <= hi:
        out.append(rs[lo])
        if lo != hi:
            out.append(rs[hi])
        lo, hi = lo + 1, hi - 1
    return out


def rate_pretty(rate):
    return f"{rate // 1000}k" if rate % 1000 == 0 else f"{rate / 1000:.1f}k"


def summarize(changes, per_change, key_fn):
    """Stall incidence grouped by some property of the change.

    Returns {key: (stalled, total)}. A capture stall at hw_params is what
    counts as "stalled": it is the one emitted by the rate change itself,
    rather than by a later recovery attempt.
    """
    out = {}
    for ch in changes:
        key = key_fn(ch)
        if key is None:
            continue
        stalled, total = out.get(key, (0, 0))
        counts = per_change.get(ch.n, {})
        out[key] = (stalled + (1 if counts.get("capture_stall_hw_params")
                               else 0), total + 1)
    return out


def main():
    c = Case()
    c.require_card()
    c.require_tools("aplay", "sox")

    rates = list(c.params.get("rates", [44100, 48000, 88200, 96000]))
    # interleave() maximizes every step and alternates direction, which is the
    # shape that provokes the fault -- but it also fixes the order, and the
    # order is what decides which hypotheses the sweep can separate. as-given
    # hands that back, so a sweep can be built to break the direction/family
    # confound that the default four rates cannot. See interleave().
    if str(c.params.get("sweep_order", "interleave")) != "as-given":
        rates = interleave(rates)
    seconds = float(c.params.get("seconds_per_rate", 1))
    loops = int(c.params.get("iterations_per_run", 10))
    device = c.device or alsa.device_name(c.card)

    with_capture = bool(c.params.get("capture", True))
    floor = float(c.params.get("min_nonzero_fraction", 0.01))
    # Idle time between changes, with nothing open. Separate from
    # seconds_per_rate on purpose -- see the module docstring.
    gap_s = float(c.params.get("gap_seconds", 0.0))
    # The steady-state rate measurement. See alsa.pointer_rate() for why this
    # replaces timing the whole aplay/arecord invocation, and why it needs
    # neither a longer run nor a smaller buffer to be accurate.
    poll_s = float(c.params.get("pointer_poll_seconds", 0.02))
    settle_s = float(c.params.get("settle_seconds", 0.5))
    plateau_s = float(c.params.get("plateau_seconds", 0.15))
    steady_min_s = float(c.params.get("steady_min_window_seconds", 1.0))
    # 5% is the target the measurement has to hit to answer "did the rate
    # change take effect", and the pointer beats it by an order of magnitude:
    # the floor is the pointer's own quantization, well under 1% over a window
    # of a second or more. Left at 5% rather than tightened to what the
    # instrument can do, because the question is a rate change, not clock
    # metrology, and a tight bound would start reporting the device's crystal.
    steady_tol = float(c.params.get("steady_tolerance", 0.05))
    # How long to wait for the card to come back before giving up on it. A
    # driver-triggered stall-and-reset recovers well inside this; it exists
    # for the device actually disappearing from the bus, which does not
    # self-heal on the timescale of a sweep. See alsa.wait_for_card_live().
    device_wait_s = float(c.params.get("device_wait_seconds", 10.0))
    changer = str(c.params.get("rate_change_stream", "capture"))
    if changer not in ("capture", "playback", "race"):
        c.blocked(f"rate_change_stream must be capture, playback or race, "
                  f"not {changer!r}")
    if changer == "capture" and not with_capture:
        c.note("rate_change_stream=capture needs a capture stream; with "
               "capture=false the rate change is necessarily performed by "
               "playback, and the run is recorded as such")
        changer = "playback"

    subs = alsa.substreams(c.card)
    pcm_p = (subs["playback"] or ["pcm0p"])[0]
    pcm_c = (subs["capture"] or ["pcm0c"])[0]

    changes = 0
    failures = 0
    first_bad_change = None
    # Generous, because a one-second measurement carries process startup and
    # device open. It is a gross-error check, not a clock-accuracy measurement.
    rate_tol = float(c.params.get("rate_tolerance", 0.20))
    # The capture ratio gets its own, much wider, threshold. Its denominator is
    # wall-clock from Popen, so it carries arecord start-up and device open and
    # reads systematically LOW by the same 10-17% measured for playback --
    # around 0.83-0.90 at four seconds, which sits right on a 0.20 tolerance.
    # Failing there would turn a known measurement bias into red runs that
    # mask the stall signal this case exists to find, so the ratio is recorded
    # at full precision and only gross error is treated as a fault.
    cap_tol = float(c.params.get("capture_rate_gross_tolerance", 0.50))
    # Below this duration the elapsed-time check cannot mean anything, so it is
    # recorded and not enforced.
    #
    # Measured on alsa-test, 2026-08-14: playing one second of audio takes
    # 1.39-1.69 s wall clock when the rate is CORRECT. That is 0.4-0.7 s of
    # sox and aplay start-up and device open, with ~0.3 s of spread on top --
    # against a 20% tolerance, i.e. 0.2 s. Enforcing the ratio there failed
    # all 20 changes of a run whose audio was, by ear, perfectly correct.
    #
    # For the ratio to carry signal the fixed cost must be small against the
    # measurement: at 4 s the same overhead is 10-17% with 7% spread, which a
    # 20% tolerance can just about hold. Shorter runs still record the number.
    timing_min_s = float(c.params.get("timing_check_min_seconds", 4.0))
    timing_enforced = seconds >= timing_min_s

    # Some steps are too small for the timing check to resolve -- 44.1 against
    # 48 kHz is 8%, inside any tolerance a one-second measurement can support.
    # That is recorded, not treated as a failure: everything else this case
    # does (stall detection, capture liveness, playback integrity) is unaffected
    # by it, so refusing to run would cost far more coverage than it protects.
    #
    # Nor is such a change untestable in principle -- it is merely not testable
    # by this instrument. A listener hears 44.1 played as 48 immediately, which
    # is what the human-verified audio cases are for.
    # Against the steady-state tolerance, not the elapsed-time one. At 20% the
    # 44.1/48 step was unresolvable and the case said so on every run; at 5%
    # it is 8.1% and plainly visible, so the whole default sweep is now
    # measurable and there is nothing to warn about.
    blind = sweep_blind_spots(rates, steady_tol)
    steady = []
    play_ratios = []
    capture_fracs = []
    marks = []
    change_log = []
    open_waits = []
    gaps = []
    capture_verdicts = {}
    bad_capture = []
    # Raw captures are kept on a bad verdict, for an operator to listen to or
    # inspect. That is unbounded on a healthy-length run, but not on an
    # endurance one: a device that wedges early and stays wedged -- exactly
    # the failure this case is built to catch -- would otherwise keep one
    # ~1 MB raw file per remaining change, thousands of them over a
    # multi-hour JT-RATE-003 run. The verdict and detail string are recorded
    # regardless; only the file past this cap is discarded.
    MAX_KEPT_BAD_CAPTURES = 50
    kept_bad_captures = 0
    prev_rate = None
    t0 = time.time()
    aborted = False

    c.progress(f"    sweep {' -> '.join(rate_pretty(r) for r in rates)}"
               f", {seconds:g}s per rate, gap {gap_s:g}s"
               f", rate change performed by {changer}"
               + ("" if with_capture else ", NO capture stream"))

    # One line per loop, per tests/README.md "Live feedback while a case runs".
    # A transient line names the rate being exercised and is replaced by the
    # loop's verdict. Reported before c.fail(), never after: the runner takes
    # the last line of stderr as the failure reason.
    total = loops * len(rates)
    for loop in range(1, loops + 1):
        bad = []
        for rate in rates:
            changes += 1
            c.status(f"    loop {loop}/{loops}  ....  {rate} Hz "
                     f"({changes}/{total} changes)")

            if not alsa.wait_for_card_live(c.card, timeout=device_wait_s):
                c.fail(f"loop {loop}, {rate} Hz: card hw:{c.card} did not "
                       f"come back within {device_wait_s:g}s -- aborting the "
                       f"sweep rather than keep spawning aplay/arecord "
                       f"against a device that is not there")
                aborted = True
                break

            # A marker per change, so a stall in the kernel log can be
            # attributed to the change that caused it rather than to the run
            # as a whole -- "when did it start" was previously unanswerable.
            # No "@": priv/jockey3-testctl rejects it, and lib/kmsg.py would
            # sanitize it to this anyway. Written the way it lands.
            mark_start = kmsg.Marker(f"{c.id}#change{changes}-{rate}")
            mark_start.write()
            marks.append(("change", changes, mark_start))

            # prev is None on the very first change, and deliberately not the
            # last rate of the sweep: the device starts at whatever rate the
            # previous run left it in, so borrowing the cyclic predecessor
            # would fabricate a transition and blur exactly the "first change
            # after probe" hypothesis the per-change record exists to test.
            step = None if prev_rate is None else abs(prev_rate / rate - 1.0)
            if prev_rate is None:
                direction = "first"
            elif rate > prev_rate:
                direction = "up"
            elif rate < prev_rate:
                direction = "down"
            else:
                direction = "same"

            raw = os.path.join(c.workdir, f"rate_{changes}_{rate}.raw")
            rec = play = gen = None
            rec_t0 = play_t0 = None
            waited = None

            # One watcher per substream, started before the process that opens
            # it so the trace covers start-up too, and never two on the same
            # substream: reading the status file zeroes avail_max and
            # overrange, so a second reader would steal what the first is
            # trying to observe.
            cap_watch = alsa.watch_pcm(c.card, pcm_c, interval=poll_s) \
                if with_capture else None
            pb_watch = alsa.watch_pcm(c.card, pcm_p, interval=poll_s)
            cap_ref = pb_ref = None

            def start_rec():
                nonlocal rec, rec_t0, cap_ref
                cap_watch.start()
                cap_ref = time.monotonic()
                rec = start_capture(device, rate, seconds, raw)
                rec_t0 = time.time()

            def start_play():
                nonlocal play, gen, play_t0, pb_ref
                pb_watch.start()
                pb_ref = time.monotonic()
                play, gen = start_playback(device, rate, seconds)
                play_t0 = time.time()

            # Ordering, not luck: whichever stream is started and confirmed
            # configured first is the one whose hw_params performs the change.
            if changer == "capture":
                start_rec()
                waited = wait_configured(cap_watch)
                start_play()
            elif changer == "playback":
                start_play()
                waited = wait_configured(pb_watch)
                if with_capture:
                    start_rec()
            else:
                if with_capture:
                    start_rec()
                start_play()
            open_waits.append((changes, waited))

            rc, err, played_s = finish_playback(play, gen, seconds, play_t0)
            if rec is not None:
                try:
                    _o, rec_err = rec.communicate(timeout=seconds + 30)
                    rec_rc = rec.returncode
                except subprocess.TimeoutExpired:
                    rec.kill()
                    rec_rc, rec_err = 124, "arecord did not finish"
                rec_elapsed = time.time() - rec_t0
                cap_watch.stop()
                verdict, frac, frames, detail = capture_outcome(
                    rec_rc, rec_err, raw, floor)
                # Effective sample rate SOURCED by the device: frames actually
                # delivered, over the wall-clock time they took to arrive.
                #
                # Not frames / requested-duration, which was the first attempt
                # and measures nothing: arecord -d derives its frame target
                # from the rate it asked for, so it returns rate x duration
                # frames whatever the device does, and a device clocking at
                # half speed simply takes twice as long. The time is where the
                # truth is, which is the whole point of checking against the
                # clock rather than against GET_RATE.
                #
                # Biased low, and knowingly: rec_elapsed starts at Popen and so
                # includes start-up and device open. See cap_tol above.
                cap_hz = (frames / rec_elapsed) if rec_elapsed > 0 else None
                cap_ratio = rate_ratio(cap_hz, rate)
                # Kept separately: did the stream deliver a full recording at
                # all? A short capture is a different fault from a slow one.
                frames_ratio = rate_ratio(frames, rate * seconds)
                capture_fracs.append(CaptureRun(
                    changes, rate, frac, frames, cap_ratio, cap_hz,
                    frames_ratio))
                capture_verdicts[verdict] = capture_verdicts.get(verdict, 0) + 1

                # The measurement that actually means something: the slope of
                # the device's own hardware pointer, taken in steady state.
                pr = alsa.pointer_rate(cap_watch.trace, cap_ref, settle_s,
                                       plateau_s, steady_min_s)
                steady.append(Steady(changes, rate, "capture", pr,
                                     cap_watch.xruns, cap_watch.hw_params))
                steady_ratio = rate_ratio(pr.hz, rate)
                if verdict == "live" and steady_ratio is not None \
                        and abs(steady_ratio - 1.0) > steady_tol:
                    verdict = "wrongrate"
                    detail = (f"the hardware pointer moved {pr.frames} frames "
                              f"in {pr.seconds:g}s of steady state, i.e. "
                              f"{pr.hz:.0f} Hz, not {rate} Hz "
                              f"(ratio {steady_ratio})")
                elif timing_enforced and verdict == "live" and pr.hz is None \
                        and cap_ratio is not None \
                        and abs(cap_ratio - 1.0) > cap_tol:
                    # Fallback only, and only for gross error: with no usable
                    # pointer window this is all there is, and it is the biased
                    # whole-invocation number.
                    verdict = "wrongrate"
                    detail = (f"no steady-state window ({pr.reason}); the "
                              f"whole-invocation estimate is {cap_hz:.0f} Hz "
                              f"against {rate} Hz (ratio {cap_ratio}), which "
                              f"is beyond gross error even allowing for its "
                              f"low bias")
                if verdict != "live":
                    if first_bad_change is None:
                        first_bad_change = changes
                    bad_capture.append((rate, verdict, detail))
                    if kept_bad_captures >= MAX_KEPT_BAD_CAPTURES:
                        try:
                            os.unlink(raw)
                        except OSError:
                            pass
                    else:
                        kept_bad_captures += 1
                else:
                    try:
                        os.unlink(raw)
                    except OSError:
                        pass
            pb_watch.stop()
            ppr = alsa.pointer_rate(pb_watch.trace, pb_ref, settle_s,
                                    plateau_s, steady_min_s)
            steady.append(Steady(changes, rate, "playback", ppr,
                                 pb_watch.xruns, pb_watch.hw_params))
            pb_steady_ratio = rate_ratio(ppr.hz, rate)
            play_ratio = rate_ratio(played_s, seconds)
            play_ratios.append((changes, rate, play_ratio))
            if rc != 0:
                failures += 1
                bad.append((rate, rc, err))
            elif pb_steady_ratio is not None:
                # The pointer is the measurement whenever there is one; the
                # elapsed-time ratio stays recorded as a cross-check but no
                # longer decides anything, because most of what it measures is
                # aplay starting up.
                if abs(pb_steady_ratio - 1.0) > steady_tol:
                    failures += 1
                    bad.append((rate, 0,
                                f"the hardware pointer consumed {ppr.frames} "
                                f"frames in {ppr.seconds:g}s of steady state, "
                                f"i.e. {ppr.hz:.0f} Hz, not {rate} Hz "
                                f"(ratio {pb_steady_ratio})"))
            elif timing_enforced and play_ratio is not None \
                    and abs(play_ratio - 1.0) > rate_tol:
                failures += 1
                bad.append((rate, 0,
                            f"no steady-state window ({ppr.reason}); played "
                            f"{seconds:g}s of audio in {played_s:.2f}s "
                            f"(ratio {play_ratio}) -- the device is not "
                            f"clocking at {rate} Hz"))

            # Both streams are shut by now. The end marker closes the change
            # window here rather than at the next change, so a reset completing
            # during the gap is attributed to the gap and not to whatever runs
            # next -- hw_params queues its reset and does not wait for it.
            mark_end = kmsg.Marker(f"{c.id}#gap{changes}")
            mark_end.write()
            marks.append(("gap", changes, mark_end))
            change_log.append(Change(
                changes, loop, rate, prev_rate, direction, step,
                None if prev_rate is None
                else clock_family(prev_rate) != clock_family(rate),
                changer, mark_start, mark_end))
            prev_rate = rate

            if gap_s > 0:
                g0 = time.time()
                time.sleep(gap_s)
                gaps.append(time.time() - g0)

        rates_ok = len(rates) - len(bad)
        line = f"{rates_ok}/{len(rates)} rates played"
        if with_capture:
            line += (", capture " + ", ".join(
                f"{v} at {r} Hz" for r, v, _ in bad_capture)
                if bad_capture else ", capture live throughout")
        c.progress(f"    loop {loop}/{loops}  "
                   f"{'FAIL' if bad or bad_capture else 'pass':4}  " + line
                   + (f", playback failed at {', '.join(str(r) for r, _, _ in bad)} Hz"
                      if bad else ""))
        for rate, verdict, detail in bad_capture:
            if verdict == "nodata":
                c.fail(f"loop {loop}, {rate} Hz: capture delivered no samples "
                       f"after the rate change ({detail}) -- the capture URB "
                       f"stream did not restart. Playback is unaffected.")
            elif verdict == "silent":
                c.fail(f"loop {loop}, {rate} Hz: capture is bit-exact silent "
                       f"after the rate change ({detail}) -- samples are "
                       f"flowing but the converter is not running.")
            elif verdict == "wrongrate":
                c.fail(f"loop {loop}, {rate} Hz: capture is clocking nowhere "
                       f"near the requested rate -- {detail}")
            else:
                c.fail(f"loop {loop}, {rate} Hz: capture failed after the rate "
                       f"change -- {detail}")
        bad_capture.clear()
        for rate, rc, err in bad:
            # This is the real failure mode: the stream did not come back
            # after the change. One is enough to fail the case, but keep
            # going -- whether it is one rate or every rate is a
            # different bug, and the counts say which.
            c.fail(f"loop {loop}, {rate} Hz: playback failed after rate "
                   f"change (exit {rc}) "
                   f"{(err or '').strip().splitlines()[-1][:100] if err else ''}")

        if aborted:
            break

    c.metric("aborted_device_unavailable", aborted)
    c.metric("rate_changes", changes)
    c.metric("failures", failures)
    c.metric("rate_check_blind_steps", len(blind))
    c.metric("timing_check_enforced", timing_enforced)
    c.metric("rate_change_stream", changer)
    c.metric("gap_seconds", gap_s)
    if gaps:
        c.metric("gap_measured_s_min", round(min(gaps), 3))
        c.metric("gap_measured_s_max", round(max(gaps), 3))
    # How long the leading stream took to reach hw_params. If this ever
    # approaches the wait_configured() timeout, the ordering that
    # rate_change_stream promises did not happen and the arm is not what it
    # says it is.
    real_waits = [w for _n, w in open_waits if w is not None]
    if real_waits:
        c.metric("lead_stream_configure_s_max", round(max(real_waits), 3))
    timeouts = sum(1 for _n, w in open_waits if w is None)
    c.metric("lead_stream_configure_timeouts", timeouts)
    # The steady-state measurements, per change and per direction. These are
    # the rate numbers that mean something; everything else timing-related in
    # this case is a cross-check kept for comparison.
    ratios = {"playback": [], "capture": []}
    startups = {"playback": [], "capture": []}
    unmeasured = {"playback": 0, "capture": 0}
    plateau_changes = {"playback": 0, "capture": 0}
    for s in steady:
        tag = f"{s.direction}_{s.n}_{s.rate}"
        c.metric(f"steady_hz_{tag}", s.pr.hz)
        c.metric(f"steady_window_s_{tag}", s.pr.seconds)
        c.metric(f"pointer_plateaus_{tag}", s.pr.plateaus)
        c.metric(f"pointer_plateau_max_s_{tag}", s.pr.plateau_max_s)
        c.metric(f"pointer_breaks_{tag}", s.pr.breaks)
        c.metric(f"pointer_tail_hold_s_{tag}", s.pr.tail_hold_s)
        c.metric(f"xruns_{tag}", s.xruns)
        # Popen to the first frame the hardware moved: the entire start-up
        # cost, measured rather than argued about. This is the quantity that
        # the whole-invocation ratio was silently charging to the sample rate.
        c.metric(f"startup_s_{tag}", s.pr.first_motion_s)
        ratio = rate_ratio(s.pr.hz, s.rate)
        c.metric(f"steady_ratio_{tag}", ratio)
        if ratio is None:
            unmeasured[s.direction] += 1
            c.metric(f"steady_unmeasured_{tag}", s.pr.reason)
        else:
            ratios[s.direction].append(ratio)
        if s.pr.first_motion_s is not None:
            startups[s.direction].append(s.pr.first_motion_s)
        if s.pr.plateaus:
            plateau_changes[s.direction] += 1
    for direction in ("playback", "capture"):
        if ratios[direction]:
            c.metric(f"steady_ratio_{direction}_min", min(ratios[direction]))
            c.metric(f"steady_ratio_{direction}_max", max(ratios[direction]))
            worst = max(ratios[direction], key=lambda r: abs(r - 1.0))
            c.metric(f"steady_error_{direction}_pct",
                     round(100.0 * abs(worst - 1.0), 2))
        if startups[direction]:
            c.metric(f"startup_s_{direction}_min", round(min(startups[direction]), 3))
            c.metric(f"startup_s_{direction}_max", round(max(startups[direction]), 3))
        c.metric(f"steady_unmeasured_{direction}", unmeasured[direction])
        c.metric(f"pointer_plateau_changes_{direction}",
                 plateau_changes[direction])
    # What ALSA negotiated, so an argument about buffering has numbers. The
    # buffer does not enter the steady-state measurement at all -- it shifts
    # latency, not rate -- but it does set how much of the start-up figure
    # above is prefill.
    for s in steady:
        if s.hw_params:
            for key in ("buffer_size", "period_size", "rate"):
                if key in s.hw_params:
                    c.metric(f"{key}_{s.direction}", s.hw_params[key])
    quanta = [s.pr.quantum for s in steady if s.pr.quantum]
    if quanta:
        c.metric("pointer_quantum_max", max(quanta))
    for n, rate, ratio in play_ratios:
        c.metric(f"playback_rate_ratio_{n}_{rate}", ratio)
    pr = [r for _, _, r in play_ratios if r is not None]
    if pr:
        c.metric("playback_rate_ratio_min", min(pr))
        c.metric("playback_rate_ratio_max", max(pr))
    if with_capture:
        # Reported apart, because they are different faults: no samples means
        # the stream never restarted, all-zero samples mean the converter did
        # not. Collapsing them once produced a verdict blaming the audio engine
        # for a transport failure, while playback was audibly fine.
        for verdict in ("live", "nodata", "silent", "error"):
            c.metric(f"capture_{verdict}_changes", capture_verdicts.get(verdict, 0))
        c.metric("first_bad_capture_change", first_bad_change)
        fracs = [r.fraction for r in capture_fracs if r.fraction is not None]
        if fracs:
            c.metric("capture_nonzero_min", min(fracs))
            c.metric("capture_nonzero_max", max(fracs))
        for r in capture_fracs:
            c.metric(f"capture_nonzero_{r.n}_{r.rate}",
                     None if r.fraction is None else round(r.fraction, 4))
            c.metric(f"capture_frames_{r.n}_{r.rate}", r.frames)
            c.metric(f"capture_effective_hz_{r.n}_{r.rate}",
                     None if r.effective_hz is None else round(r.effective_hz))
            c.metric(f"capture_rate_ratio_{r.n}_{r.rate}", r.rate_ratio)
            c.metric(f"capture_frames_ratio_{r.n}_{r.rate}", r.frames_ratio)
        seen = [r.rate_ratio for r in capture_fracs if r.rate_ratio is not None]
        if seen:
            c.metric("capture_rate_ratio_min", min(seen))
            c.metric("capture_rate_ratio_max", max(seen))
    # Summed from the watchers, which sample WHILE the streams are open. The
    # old before-and-after reading of alsa.xruns() could only ever be zero:
    # the status file reports "closed" with nothing open, pcm_status() returns
    # {}, and the difference of two absent values is no difference at all --
    # lib/alsa.py says so in as many words. This metric has been reporting a
    # clean run regardless of what happened for as long as it has existed.
    #
    # It is not decoration here. An xrun inside a measurement window makes that
    # window's rate meaningless, so it has to be visible next to the rate.
    c.metric("xruns", sum(s.xruns for s in steady))
    c.metric("elapsed_s", round(time.time() - t0, 1))
    if changes:
        c.metric("failure_rate_pct", round(100.0 * failures / changes, 2))

    # Report what the driver logged, while the run is still on screen. The
    # runner's classifier produces the same totals, but only into run.json
    # after the case has exited -- so an operator watching a two-minute sweep
    # had no idea whether it was provoking any stalls at all.
    #
    # A single read of a finite ring buffer, taken after a run that can run
    # for hours (JT-RATE-003): on a long enough run the early part of the log
    # -- including some of the markers this attribution depends on -- can be
    # gone by the time this line runs. That degrades gracefully rather than
    # silently: markers_found_in_log/markers_written and
    # attribution_trustworthy below say so, and totals are then a LOWER bound
    # (events that scrolled out are undercounted, never double-counted) on
    # resets_per_change_pct and its companions. See re/rate_change_stall.md.
    log = kmsg.read_log()
    # Every window this case reports rests on the markers having landed. When
    # they have not, the windows do not merely lose detail -- they SHIFT, and
    # each change is charged with its neighbour's events. That is how a run
    # once reported "capture stalled on 0/20 changes" next to six resets.
    #
    # So attribution is reported only when every marker is accounted for, and
    # the run says plainly when it is not. Silence here is worse than a gap.
    missing = [n for _kind, n, mk in marks if not mk.written]
    found = sum(1 for _kind, _n, mk in marks
                if mk.written and any(mk.token in line for line in log))
    attributable = not missing and found == len(marks)
    c.metric("markers_written", len(marks) - len(missing))
    c.metric("markers_found_in_log", found)
    c.metric("attribution_trustworthy", attributable)
    per_change = {}
    gap_counts = {}
    totals = {}
    for kind, n, lines in window_lines(log, marks):
        counts = count_events(classify_events(lines))
        if kind == "change":
            per_change[n] = counts
        else:
            gap_counts[n] = counts
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + v

    branches = {}
    stalled_at = []
    for ch in change_log:
        counts = per_change.get(ch.n, {})
        branch = branch_of(counts)
        branches[branch] = branches.get(branch, 0) + 1
        c.metric(f"branch_change_{ch.n}_{ch.rate}", branch)
        c.metric(f"transition_change_{ch.n}",
                 f"{ch.prev or 'start'}->{ch.rate}")
        for k, v in counts.items():
            c.metric(f"{k}_change_{ch.n}_{ch.rate}", v)
        for k, v in gap_counts.get(ch.n, {}).items():
            c.metric(f"{k}_gap_{ch.n}", v)
        if counts.get("capture_stall_hw_params"):
            stalled_at.append(f"{ch.n}@{rate_pretty(ch.rate)}")
    for k, v in totals.items():
        c.metric(f"{k}_total", v)
    for k, v in branches.items():
        c.metric(f"branch_{k}", v)

    # The three groupings that open question 2 of re/rate_change_stall.md asks
    # for. They cost nothing beyond the arithmetic and have never been looked
    # at, because until the per-change attribution existed they could not be.
    by_direction = summarize(change_log, per_change, lambda ch: ch.direction)
    by_pair = summarize(change_log, per_change,
                        lambda ch: None if ch.prev is None
                        else f"{ch.prev}->{ch.rate}")
    by_family = summarize(change_log, per_change,
                          lambda ch: None if ch.family_cross is None
                          else ("cross" if ch.family_cross else "within"))
    by_loop = summarize(change_log, per_change, lambda ch: ch.loop)
    # Emitted as a count and a denominator rather than as "9/20": ledger.py
    # trends numeric metrics and skips everything else, and "does the stall
    # rate drift" is the question JT-RATE-003 exists to ask. The readable form
    # goes on screen instead.
    for group, name in ((by_direction, "direction"), (by_pair, "pair"),
                        (by_family, "family")):
        for key, (s, t) in group.items():
            label = str(key).replace("->", "_to_")
            c.metric(f"stalls_{name}_{label}", s)
            c.metric(f"changes_{name}_{label}", t)

    n_stalled = len(stalled_at)
    # Every reset below is the same operation -- usb_queue_reset_device() -- so
    # they are shown as one count with its three routes underneath, rather than
    # as separate counts that look like different severities. The previous
    # wording put "resets 9" beside "escalated to full reset 0" and read as
    # though nine resets had happened and none of them had been full.
    #
    # Since Stage 3 unified recovery, ALL THREE routes try the same cheap URB
    # restart first (jockey3_recover_urb_stream()) and only escalate to a full
    # reset if that alone did not work -- including the direct-from-hw_params
    # route, which used to reset unconditionally with no cheap step at all.
    direct = totals.get("reset_on_rate_change", 0)
    after_capture_restart = totals.get("reset_after_urb_restart", 0)
    after_playback_prepare = totals.get("reset_after_playback_prepare", 0)
    c.progress(
        f"    driver: capture stalled at hw_params on "
        f"{n_stalled if attributable else '?'}/{changes} changes"
        f", playback stalls {totals.get('playback_stall', 0)}")
    c.progress(
        f"    full USB resets: {direct + after_capture_restart + after_playback_prepare}"
        f"  ({direct} after hw_params()'s own URB restart failed"
        f", {after_capture_restart} after prepare()'s capture-open restart failed"
        f", {after_playback_prepare} after prepare()'s playback restart failed)")
    if totals.get("prepare_capture"):
        c.progress(
            f"    deferred recovery at capture open: "
            f"{totals.get('prepare_capture', 0)} picked up"
            f", {max(0, totals.get('prepare_capture', 0) - after_capture_restart)} "
            f"cleared by a URB restart alone")
    if totals.get("recovery_budget_exhausted"):
        c.progress(
            f"    recovery budget exhausted (gave up without resetting): "
            f"{totals['recovery_budget_exhausted']}")
    if totals.get("stalled_after_full_reset"):
        c.progress(f"    STILL STALLED after a full reset: "
                   f"{totals['stalled_after_full_reset']}")
    for direction in ("playback", "capture"):
        if not ratios[direction]:
            continue
        worst = max(ratios[direction], key=lambda r: abs(r - 1.0))
        c.progress(f"    {direction} clock: ratio "
                   f"{min(ratios[direction]):.3f}-{max(ratios[direction]):.3f}"
                   f", worst error {abs(worst - 1.0):.2%} against a "
                   f"{steady_tol:.0%} bound"
                   + (f", {unmeasured[direction]} unmeasurable"
                      if unmeasured[direction] else "")
                   + (f", startup {min(startups[direction]):.2f}"
                      f"-{max(startups[direction]):.2f}s excluded"
                      if startups[direction] else ""))
    if not attributable:
        # Deliberately NOT printed as zeros. A table of "0/10" that is really
        # "unknown" is the most expensive output this case can produce: it
        # reads as a result and it is not one.
        c.progress(f"    per-change attribution UNAVAILABLE: "
                   f"{len(marks) - found} of {len(marks)} markers did not "
                   f"reach the kernel log. Run totals above are still valid; "
                   f"every by-change, by-direction and by-pair figure is "
                   f"suppressed rather than shown wrong.")
    else:
        c.progress("    branches: " + ", ".join(
            f"{k} {v}" for k, v in sorted(branches.items())))
        c.progress("    by direction: " + ", ".join(
            f"{k} {s}/{t}" for k, (s, t) in sorted(by_direction.items())))
        c.progress("    by pair: " + ", ".join(
            f"{rate_pretty(int(k.split('->')[0]))}->"
            f"{rate_pretty(int(k.split('->')[1]))}"
            f" {s}/{t}" for k, (s, t) in sorted(by_pair.items())))
        if by_family:
            c.progress("    by clock family: " + ", ".join(
                f"{k} {s}/{t}" for k, (s, t) in sorted(by_family.items())))
        if len(by_loop) > 1:
            c.progress("    by loop: " + ", ".join(
                f"{k}:{s}/{t}" for k, (s, t) in sorted(by_loop.items())))
        if stalled_at:
            c.progress("    stalled at: " + ", ".join(stalled_at))
    gap_events = sum(v for counts in gap_counts.values()
                     for v in counts.values())
    if gap_events:
        c.progress(f"    {gap_events} driver events landed between changes "
                   f"rather than during one -- mostly resets completing after "
                   f"the streams closed")

    if not attributable:
        c.note(
            f"{len(marks) - found} of {len(marks)} kernel-log markers are "
            f"missing, so windows would be shifted rather than merely coarse "
            f"and every per-change figure is suppressed. Check that "
            f"priv/jockey3-testctl is installed and that the marker labels "
            f"only use the charset it accepts -- it rejects anything else "
            f"silently, and lib/kmsg.py sanitizes labels for exactly that "
            f"reason.")
    # ------------------------------------------------------ the key metric
    #
    # How often a rate change costs a USB device reset. This is the number to
    # watch while the driver is being worked on: it should trend to zero, and a
    # release where it does not has not fixed the fault, only survived it.
    #
    # All four routes a reset can be queued from count, because they are the
    # same event from the device's point of view -- an audible interruption to
    # whatever was playing:
    #
    #   reset_on_rate_change
    #              hw_params()'s own call to jockey3_recover_urb_stream()
    #              (context "rate change") tried the lightweight URB restart
    #              first, it did not work, and it queued a full reset
    #   reset_after_urb_restart
    #              prepare()'s capture-open call did the same and also failed
    #   reset_after_playback_prepare
    #              prepare()'s playback call did the same and also failed
    #              recovery, so it could never have contributed a reset
    #   reset_on_watchdog
    #              jockey3_watchdog_work()'s own call (context "watchdog")
    #              did the same and also failed -- not tied to a rate change
    #              or a PCM open, so it can fire on a change that never
    #              touched hw_params()'s or prepare()'s own recovery path.
    #              Missing here until 2026-08-26: every reset this route
    #              queued was silently uncounted from when the watchdog
    #              gained it (`e780ef4`, "make the URB liveness watchdog
    #              self-healing", 2026-08-23) until this line was added. Any
    #              resets_total_device/resets_per_change_pct recorded for a
    #              build after `e780ef4` and before this fix is a lower
    #              bound, not a measurement -- see re/rate_change_stall.md.
    #
    # A URB restart that DID work is not counted: it is the outcome this metric
    # wants more of, so it is reported beside the percentage rather than inside
    # it. Nor is a give-up on an exhausted recovery budget (recovery_budget_exhausted)
    # counted here -- it deliberately did NOT reset the device, so counting it
    # here would conflate "recovery did not run" with "recovery ran and reset".
    #
    # Taken from the run totals, not from the per-change attribution, and
    # deliberately so: totals survive a marker that failed to land, and on
    # 2026-08-15 that was the difference between a headline of 30% and a
    # headline of zero. It is the one figure here that must not be able to go
    # quietly wrong.
    resets = (totals.get("reset_on_rate_change", 0)
              + totals.get("reset_after_urb_restart", 0)
              + totals.get("reset_after_playback_prepare", 0)
              + totals.get("reset_on_watchdog", 0))
    light_retries = (totals.get("hw_params_light_retry", 0)
                     + totals.get("prepare_capture", 0)
                     + totals.get("prepare_playback", 0))
    restarts_that_worked = max(0, light_retries - resets
                               - totals.get("recovery_budget_exhausted", 0))
    reset_pct = round(100.0 * resets / changes, 1) if changes else None
    stall_pct = (round(100.0 * totals.get("capture_stall_hw_params", 0)
                       / changes, 1) if changes else None)
    c.metric("resets_per_change_pct", reset_pct)
    c.metric("resets_total_device", resets)
    c.metric("stalls_per_change_pct", stall_pct)
    c.metric("urb_restarts_that_avoided_a_reset", restarts_that_worked)

    c.progress("")
    c.progress(f"    ==> RESETS PER RATE CHANGE: {reset_pct}%   "
               f"({resets} reset{'' if resets == 1 else 's'} over {changes} "
               f"changes)   [target 0%, {changer} arm]")
    # The decomposition, because the headline alone cannot say which way it
    # moved. Fewer stalls and fewer resets is the fix; the same stalls handled
    # by a URB restart instead of a reset is progress too, and would otherwise
    # look identical to no change at all.
    stalls = totals.get("capture_stall_hw_params", 0)
    detail = f"        capture stalled after {stall_pct}% of changes"
    if stalls:
        detail += (f", of which {round(100.0 * min(resets, stalls) / stalls)}%"
                   f" cost a device reset")
    elif resets:
        detail += (" -- so these resets came from a playback stall, not a "
                   "capture one")
    if restarts_that_worked:
        detail += (f"; {restarts_that_worked} recovered on a URB restart "
                   f"alone, with no reset")
    c.progress(detail)
    if changer == "capture":
        # Said out loud because the alternative is being read as a result.
        # prepare()'s capture-open path (prepare_capture / reset_after_urb_restart)
        # is reached only when capture was NOT open at the moment hw_params()
        # ran, which this arm never allows -- capture is always open first, so
        # hw_params() itself always finds and recovers the stall. Both counters
        # are therefore structurally zero here, not evidence the deferred path
        # works or does not. Since Stage 3, hw_params() ALSO tries the cheap URB
        # restart before a reset (hw_params_light_retry / reset_on_rate_change),
        # so "resets on the spot" no longer describes this arm either -- what is
        # unreachable here is specifically prepare()'s capture-open counters.
        c.progress("        comparable only against another capture-arm run: "
                   "prepare()'s deferred capture-open recovery path is "
                   "unreachable here, so its counters are structurally zero")
    if totals.get("stalled_after_full_reset"):
        c.progress(f"        {totals['stalled_after_full_reset']} did not recover even "
                   f"after a full reset")
    if not with_capture:
        c.progress("        NOTE capture was never opened, so a capture stall "
                   "is deferred and never resets. This arm cannot produce the "
                   "metric above; 0% here means untested, not fixed.")
    c.progress("")

    c.note("stall and reset-delay counts come from the kernel log; a stall "
           "that recovered is expected and is not a failure")
    c.note(f"the rate change was performed by the {changer} stream on every "
           f"change, which fixes which branch of jockey3_pcm_hw_params() is "
           f"under test: {changer} means "
           + ("capture_open=1, so a capture stall is recovered by hw_params() "
              "itself, on the spot"
              if changer == "capture" else
              "capture_open=0, so a capture stall is deferred to the next "
              "capture open" if changer == "playback" else
              "the branch is whichever process won the race, and varies "
              "within the run -- read branch_change_* before comparing this "
              "run against another"))
    if not with_capture:
        c.note("no capture stream was opened, so nothing ever recovered a "
               "deferred capture stall. Once capture dies it stays dead and "
               "hw_params re-logs the stall at every later change: the stall "
               "count here is one outage re-detected, NOT per-change "
               "incidence, and must not be compared against a capture:true "
               "run as though it were.")
    if branches.get("reset") and (totals.get("prepare_capture")
                                  or totals.get("reset_after_urb_restart")):
        c.note("branch_reset is a precedence label, not a clean count: "
               "hw_params queues its reset without waiting, so the prepare "
               "that follows can start the URB-restart ladder on top of a "
               "reset already in flight. Read "
               "reset_after_urb_restart_total and "
               "prepare_capture_total beside it.")
    if timeouts:
        c.note(f"{timeouts} of {changes} changes did not see the leading "
               f"{changer} stream reach hw_params within the wait, so the "
               f"ordering rate_change_stream promises was not achieved there "
               f"and those changes may have taken the other branch. "
               f"branch_change_* records what each one actually did.")
    c.note(
        f"the rate is measured from the device's own hardware pointer in "
        f"steady state -- {settle_s:g}s after the pointer starts moving, "
        f"across the longest span of at least {steady_min_s:g}s in which it "
        f"never stops -- and judged at {steady_tol:.0%}. Start-up, device open "
        f"and every buffer in the path are outside that window rather than "
        f"averaged into it, and buffer size does not enter it at all: a buffer "
        f"shifts latency, not rate. The elapsed-time ratios "
        f"(playback_rate_ratio_*, capture_rate_ratio_*) are kept as "
        f"cross-checks and no longer decide anything while a pointer "
        f"measurement exists.")
    startup_all = [s for d in ("playback", "capture") for s in startups[d]]
    if startup_all:
        c.note(
            f"startup_s_* is that excluded cost, measured: {min(startup_all):.2f}"
            f"-{max(startup_all):.2f}s from process start to the first frame "
            f"the hardware moved. Against a {seconds:g}s run that is "
            f"{100.0 * min(startup_all) / seconds:.0f}-"
            f"{100.0 * max(startup_all) / seconds:.0f}% -- which is exactly "
            f"the low bias the whole-invocation ratios carry, and it is now "
            f"reported rather than mistaken for a slow clock.")
    if sum(unmeasured.values()):
        c.note(
            f"{sum(unmeasured.values())} direction-changes had no usable "
            f"steady-state window and fell back to the elapsed-time estimate "
            f"at a {cap_tol:.0%} gross-error bound. steady_unmeasured_* says "
            f"why for each; the usual reason is a pointer that stopped, which "
            f"is the stall itself and is counted as such.")
    if sum(plateau_changes.values()):
        c.note(
            f"the hardware pointer stopped and later resumed on "
            f"{plateau_changes['capture']} capture and "
            f"{plateau_changes['playback']} playback measurements. That is a "
            f"stall observed directly, at the {poll_s * 1000:.0f} ms poll "
            f"interval rather than at the watchdog's one second, and with "
            f"onset and duration attached -- see pointer_plateau_max_s_*.")
    if not timing_enforced:
        c.note(
            f"the elapsed-time cross-check ran at {seconds:g}s per rate, below "
            f"the {timing_min_s:g}s at which it means anything. It is recorded "
            f"only. This no longer costs the case its rate check: the pointer "
            f"measurement is unaffected by duration, needing only "
            f"{settle_s + steady_min_s:g}s of stream to work.")
    if blind:
        c.note(
            f"{len(blind)} of this sweep's steps are smaller than the "
            f"{steady_tol:.0%} tolerance -- "
            + "; ".join(f"{a}->{b} is {d:.1%}" for a, b, d in blind)
            + ". A device that ignored those changes would not be detected "
            "here. Use rates from opposite ends, e.g. [44100, 96000], to make "
            "the step measurable.")
    c.done()


if __name__ == "__main__":
    main()
