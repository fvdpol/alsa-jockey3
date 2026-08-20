# SPDX-License-Identifier: GPL-2.0-or-later
"""ALSA-side helpers: find the card, read its state, count xruns.

Everything here reads /proc/asound rather than shelling out, because the
numbers are what tests assert on and parsing `aplay` output for them is
fragile in a way that quietly passes.
"""

import glob
import os
import re
import subprocess
import threading
import time

DRIVER_ID = "RJ3"               # jockey3.c: snd_card_set_id(card, "RJ3")


def _read(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def find_card():
    """Return (index, id) of the Jockey 3 card, or (None, None).

    Matched by card id rather than by index: index depends on what else is
    plugged in, and hardcoding hw:1 is how a test ends up driving the onboard
    audio and passing.
    """
    for path in sorted(glob.glob("/proc/asound/card[0-9]*")):
        if not os.path.isdir(path):
            continue
        idx = os.path.basename(path)[len("card"):]
        cid = _read(os.path.join(path, "id"))
        if cid and DRIVER_ID.lower() in cid.lower():
            return int(idx), cid
    # Fall back to the card list, which names the driver even when the id
    # differs.
    for line in _read("/proc/asound/cards").splitlines():
        m = re.match(r"\s*(\d+)\s+\[(\S+)\s*\]", line)
        if m and DRIVER_ID.lower() in line.lower():
            return int(m.group(1)), m.group(2)
    return None, None


def device_name(index, dev=0):
    return f"hw:{index},{dev}"


def control_is_live(index):
    """Is this card's control node backed by a card that is actually there?

    A stale /dev/snd/controlCN node left behind by a departed card still
    stats fine; opening it is what tells the two apart, and it is also
    precisely what alsa-lib does when resolving "hw:<index>,0" -- so this
    asks the same question aplay/arecord will. /proc/asound/cardN and its
    "id" file are created early in probe, before snd_card_register()
    publishes the character devices, so presence there is not proof the
    node is openable yet -- and the character devices from a card's
    previous incarnation can survive a disconnect until udev reaps them,
    which is asynchronous and slower than probe.
    """
    try:
        fd = os.open(f"/dev/snd/controlC{index}", os.O_RDONLY)
    except OSError:
        return False
    os.close(fd)
    return True


def wait_for_card_live(index, timeout=10.0, poll=0.5):
    """Block until control_is_live(index), or timeout expires.

    For long endurance sweeps (JT-RATE-003 and friends) that open a fresh
    aplay/arecord per iteration for hours. A driver-triggered stall-and-reset
    recovers in about a second and control_is_live() stays true throughout it
    -- that case does not need this and pays only one cheap open() per
    change. What this exists for is the device actually disappearing from the
    bus: 2026-08-20 found an arm64 rig stay unresponsive for 18 minutes after
    one, needing a physical power-cycle, while the sweep kept spawning
    arecord/aplay against nothing the whole time and every failure it logged
    for that stretch was noise. A bounded wait here turns that into "recovers
    within timeout, keep going" or "still gone, the caller should give up
    early" instead of a run that discovers hours later it learned nothing.

    Returns True if the card was (or became) live, False on timeout.
    """
    deadline = time.time() + timeout
    while True:
        if control_is_live(index):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(poll)


def substreams(index):
    """What the card actually exposes -- the shape probe should have created."""
    base = f"/proc/asound/card{index}"
    out = {"playback": [], "capture": [], "rawmidi": []}
    for p in sorted(glob.glob(os.path.join(base, "pcm*p"))):
        out["playback"].append(os.path.basename(p))
    for p in sorted(glob.glob(os.path.join(base, "pcm*c"))):
        out["capture"].append(os.path.basename(p))
    for p in sorted(glob.glob(os.path.join(base, "midi*"))):
        out["rawmidi"].append(os.path.basename(p))
    return out


# The field is "xrun_counter" in current kernels; "xruns" is accepted too in
# case an older one spells it that way. Matching only "xruns" is how this
# metric read zero on every run for a day: the regex never matched, and a
# missing key defaulted to 0, which is indistinguishable from a clean run.
XRUN_RE = re.compile(r"^\s*xrun(?:s|_counter)\s*:\s*(\d+)", re.M)
STATE_RE = re.compile(r"^\s*state\s*:\s*(\S+)", re.M)
AVAIL_MAX_RE = re.compile(r"^\s*avail_max\s*:\s*(\d+)", re.M)
DELAY_RE = re.compile(r"^\s*delay\s*:\s*(-?\d+)", re.M)
# The hardware pointer: total frames the DEVICE has moved since the stream
# started, which is the only quantity here that is the device's own clock
# rather than something measured about a process. Reading this file calls
# snd_pcm_update_hw_ptr() while the stream is running, so the value is fresh
# as of the read rather than as of the last URB the reader happened to miss.
#
# NOTE the neighbouring "tstamp" field is deliberately not parsed.
# snd_pcm_status64() fills it only when the application enabled
# SNDRV_PCM_TSTAMP_ENABLE, which aplay and arecord do not, so it reads
# 0.000000000 and pairing it with hw_ptr would divide by garbage. Callers pair
# hw_ptr with their own CLOCK_MONOTONIC reading instead; the file read is the
# only latency between the two, and it is microseconds.
HW_PTR_RE = re.compile(r"^\s*hw_ptr\s*:\s*(\d+)", re.M)
APPL_PTR_RE = re.compile(r"^\s*appl_ptr\s*:\s*(\d+)", re.M)


def pcm_status(index, pcm="pcm0p", sub="sub0"):
    """Parse a substream's status file.

    Returns {} when the substream is closed -- the status file exists but
    reports 'closed', which is not an error condition.

    IMPORTANT: everything here is readable only while the substream is OPEN.
    Sampling before and after a playback command therefore reads 'closed'
    twice and reports a difference of zero no matter what happened in
    between. Use watch_pcm() to sample during.
    """
    path = f"/proc/asound/card{index}/{pcm}/{sub}/status"
    text = _read(path)
    if not text or text.startswith("closed"):
        return {}
    out = {}
    for key, rx in (("xruns", XRUN_RE), ("avail_max", AVAIL_MAX_RE),
                    ("delay", DELAY_RE), ("hw_ptr", HW_PTR_RE),
                    ("appl_ptr", APPL_PTR_RE)):
        m = rx.search(text)
        if m:
            out[key] = int(m.group(1))
    m = STATE_RE.search(text)
    if m:
        out["state"] = m.group(1)
    return out


HW_PARAM_RE = re.compile(r"^\s*(\w+)\s*:\s*(\S+)", re.M)


def hw_params(index, pcm="pcm0p", sub="sub0"):
    """The parameters ALSA actually negotiated, while the substream is open.

    Readable only during the stream, like status. Worth recording alongside a
    rate measurement because it says what the buffer and period actually came
    out as -- the numbers an argument about buffering otherwise has to guess
    at. Values stay strings except the ones that are plainly counts.
    """
    text = _read(f"/proc/asound/card{index}/{pcm}/{sub}/hw_params")
    if not text or text.startswith("closed"):
        return {}
    out = {}
    for key, value in HW_PARAM_RE.findall(text):
        if value.isdigit():
            out[key] = int(value)
        else:
            out[key] = value
    return out


def xruns(index, pcm="pcm0p", sub="sub0"):
    return pcm_status(index, pcm, sub).get("xruns", 0)


class watch_pcm(threading.Thread):
    """Sample a substream's status for as long as it stays open.

    The counters vanish when the stream closes, so they have to be read while
    it runs. Returns the last values seen rather than the first: xrun_counter
    only rises, and avail_max is a high-water mark the kernel keeps for us.

    avail_max is worth as much as the xrun count here. It is how close the
    buffer came to running dry, so it distinguishes "comfortable" from "made
    it by one period" -- and a run that never xruns but whose avail_max climbs
    toward buffer_size is the one about to start crackling.

    It also keeps a trace of (monotonic time, hw_ptr). That pair is the device
    clock seen from outside: hw_ptr counts frames the hardware has moved, so
    its slope is the sample rate the device is really running at, with process
    start-up, device open and every buffer in the path outside the measurement
    rather than inside it. A plateau in the same trace is a stall, observed
    directly and at the poll interval rather than inferred from the kernel log
    at the watchdog's one-second resolution.

    ONE WATCHER PER SUBSTREAM. Reading the status file is not free of side
    effects: snd_pcm_status64() ends by zeroing avail_max and overrange, so two
    watchers on one substream would each reset what the other was trying to
    observe. Different substreams are independent and may be watched at once.
    """

    def __init__(self, index, pcm="pcm0p", sub="sub0", interval=0.02):
        super().__init__(daemon=True)
        self.index, self.pcm, self.sub = index, pcm, sub
        self.interval = interval
        self._stop = threading.Event()
        self.samples = 0
        self.xruns = 0
        self.avail_max = 0
        self.saw_open = False
        self.trace = []            # (time.monotonic(), hw_ptr)
        self.hw_params = {}        # captured once, while the stream is open
        # The last state seen. Published so that a caller waiting for the
        # substream to reach a state does not have to open a second reader on
        # it: status reads are not side-effect free (see the class docstring),
        # so the watcher is the one reader and everyone else asks it.
        self.state = None

    def run(self):
        while not self._stop.is_set():
            st = pcm_status(self.index, self.pcm, self.sub)
            if st:
                self.saw_open = True
                self.samples += 1
                self.state = st.get("state")
                self.xruns = max(self.xruns, st.get("xruns", 0))
                self.avail_max = max(self.avail_max, st.get("avail_max", 0))
                if "hw_ptr" in st:
                    # Timestamped here rather than from the file: the kernel's
                    # own tstamp is left at zero unless the application asked
                    # for it, and aplay and arecord do not. See HW_PTR_RE.
                    self.trace.append((time.monotonic(), st["hw_ptr"]))
                if not self.hw_params:
                    self.hw_params = hw_params(self.index, self.pcm, self.sub)
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        self.join(timeout=2)
        return self


class PointerRate:
    """What a hw_ptr trace says about the rate the device is really clocking at.

    Attributes:
      hz               frames per second across the measurement window, or None
      frames, seconds  what that was measured over
      window_start_s   where the window began, relative to the reference
      first_motion_s   reference to the first frame the device moved: the
                       whole start-up cost -- exec, open, hw_params, prepare
                       and the first buffer -- measured rather than apportioned
      plateaus         runs where the pointer stopped for longer than plateau_s
      plateau_max_s    the longest of them
      breaks           discontinuities: the pointer went backwards, which is a
                       stream restart, not a stall
      tail_hold_s      how long the pointer was still at the end of the trace.
                       The stream stopping, not stalling -- excluded from the
                       window but deliberately not counted as a plateau
      quantum          median frames per observed step -- the resolution floor
      reason           why hz is None, when it is
    """

    __slots__ = ("hz", "frames", "seconds", "window_start_s", "first_motion_s",
                 "plateaus", "plateau_max_s", "breaks", "quantum",
                 "tail_hold_s", "reason")

    def __init__(self, **kw):
        for name in self.__slots__:
            setattr(self, name, kw.get(name))

    def __repr__(self):
        return (f"PointerRate(hz={self.hz}, seconds={self.seconds}, "
                f"plateaus={self.plateaus}, reason={self.reason!r})")


def pointer_rate(trace, ref_t=None, settle_s=0.5, plateau_s=0.15,
                 min_window_s=1.0):
    """Measure the device's sample rate from a (time, hw_ptr) trace.

    Timing a whole aplay or arecord invocation charges process start-up, device
    open and every buffer in the path to the sample rate: the result is
    rate / (1 + overhead/duration), which reads low by 10-17% at four seconds
    and can only be improved by making the run longer. hw_ptr counts frames the
    HARDWARE has moved, so its slope is the device's clock with all of that
    outside the measurement. Buffer size does not enter it at all -- a buffer
    shifts latency, not rate -- which is why this needs no buffer tuning and no
    custom ALSA client to beat a 5% target by an order of magnitude.

    Two things are excluded from the window rather than averaged into it:

      settle_s    the first stretch after the pointer starts moving, where
                  prefill and the first URBs have not reached steady state
      plateaus    stretches where the pointer stopped. A stall dragged through
                  the average reads as a slow clock, which would report the
                  fault this driver actually has as the fault it does not --
                  and put it in the same bucket as a genuine rate error.

    So the rate is measured across the longest plateau-free span of the steady
    region, and the plateaus are reported separately as what they are. A
    backwards step is treated as a break rather than a stall: hw_ptr restarts
    at zero after a device reset, which on this driver is a routine event.
    """
    if not trace or len(trace) < 2:
        return PointerRate(reason="no pointer samples", plateaus=0, breaks=0,
                           plateau_max_s=0.0)

    ts = [t for t, _p in trace]
    ps = [p for _t, p in trace]
    ref = ref_t if ref_t is not None else ts[0]

    move = next((i for i in range(1, len(ps)) if ps[i] > ps[i - 1]), None)
    if move is None:
        return PointerRate(reason="the pointer never advanced", plateaus=0,
                           breaks=0, plateau_max_s=0.0,
                           first_motion_s=None)
    first_motion_s = round(ts[move - 1] - ref, 3)

    # Cuts: a plateau longer than plateau_s, or the pointer going backwards.
    #
    # Scanned from the first motion, never from the start of the trace. The
    # samples before the device moves its first frame are the stream opening,
    # and they are static for as long as start-up takes -- 300-500 ms, which is
    # comfortably longer than plateau_s. Counting that run would report a stall
    # on every single change, and the number would look plausible.
    cuts = []
    plateaus = []
    breaks = 0
    static_from = None
    for i in range(move, len(ps)):
        if ps[i] < ps[i - 1]:
            breaks += 1
            cuts.append((i - 1, i))
            static_from = None
        elif ps[i] > ps[i - 1]:
            if static_from is not None:
                held = ts[i - 1] - ts[static_from]
                if held > plateau_s:
                    plateaus.append(held)
                    cuts.append((static_from, i - 1))
                static_from = None
        elif static_from is None:
            static_from = i - 1
    # A static run that reaches the end of the trace is the stream stopping,
    # not stalling: the process has finished and the substream is closing, and
    # the watcher is simply still sampling. It is cut out of the measurement
    # like any other plateau, but it is NOT counted as one -- otherwise every
    # healthy change reports a stall, which would make the plateau count
    # useless for the thing it exists to detect. Reported separately instead.
    tail_hold_s = 0.0
    if static_from is not None:
        tail_hold_s = round(ts[-1] - ts[static_from], 3)
        if tail_hold_s > plateau_s:
            cuts.append((static_from, len(ps) - 1))

    steps = [ps[i] - ps[i - 1] for i in range(1, len(ps)) if ps[i] > ps[i - 1]]
    steps.sort()
    quantum = steps[len(steps) // 2] if steps else None

    # Segments between the cuts, then the steady part of each. Also from the
    # first motion: nothing before it can carry a rate.
    start = move - 1
    segments = []
    for a, b in sorted(cuts):
        if a > start:
            segments.append((start, a))
        start = max(start, b)
    if start < len(ps) - 1:
        segments.append((start, len(ps) - 1))

    steady_from = ts[move - 1] + settle_s
    best = None
    for a, b in segments:
        while a < b and ts[a] < steady_from:
            a += 1
        if a >= b:
            continue
        span = ts[b] - ts[a]
        if span >= min_window_s and (best is None or span > best[2]):
            best = (a, b, span)

    common = dict(plateaus=len(plateaus), breaks=breaks, quantum=quantum,
                  first_motion_s=first_motion_s, tail_hold_s=tail_hold_s,
                  plateau_max_s=round(max(plateaus), 3) if plateaus else 0.0)
    if best is None:
        return PointerRate(
            reason=(f"no plateau-free window of {min_window_s:g}s after "
                    f"{settle_s:g}s of settling"), **common)
    a, b, span = best
    frames = ps[b] - ps[a]
    return PointerRate(hz=round(frames / span, 1), frames=frames,
                       seconds=round(span, 3),
                       window_start_s=round(ts[a] - ref, 3), **common)


def rawmidi_device(index):
    for p in sorted(glob.glob(f"/proc/asound/card{index}/midi*")):
        name = os.path.basename(p)
        m = re.match(r"midi(\d+)", name)
        if m:
            return f"hw:{index},{m.group(1)}"
    return None


def rawmidi_node(index):
    """Character device path for the same port rawmidi_device() names.

    For a direct open()/write() -- no amidi subprocess per call -- when a
    case needs to measure throughput rather than send a few fixed messages.
    """
    for p in sorted(glob.glob(f"/proc/asound/card{index}/midi*")):
        name = os.path.basename(p)
        m = re.match(r"midi(\d+)", name)
        if m:
            return f"/dev/snd/midiC{index}D{m.group(1)}"
    return None


def have(tool):
    from shutil import which
    return which(tool) is not None


def missing_tools(tools):
    return [t for t in tools if not have(t)]


def active_sound_servers():
    """Names of sound-server processes currently running, if any.

    A report, not an action. PipeWire, JACK (jackd/jackdbus) and anything
    built on either -- WirePlumber, or Mixxx routed through JACK instead of
    ALSA -- can hold the card open and turn an exclusive-access test failure
    into something that looks like a driver bug. Stopping pipewire.service
    used to be automatic here, but pipewire.socket respawns it the moment
    anything -- including PipeWire's own ALSA monitor reacting to a card
    appearing mid cold-boot test -- touches the socket, so a stop is not
    reliable without also masking, and masking JACK correctly is not this
    script's call to make: it may be serving something else on this machine
    entirely. See tests/hw/actions/sound_server.sh for the operator-driven
    disable/enable.
    """
    found = []
    for name in ("pipewire", "wireplumber", "jackd", "jackdbus"):
        r = subprocess.run(["pgrep", "-x", name], capture_output=True)
        if r.returncode == 0:
            found.append(name)
    return found
