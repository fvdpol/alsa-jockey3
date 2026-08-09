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

DRIVER_ID = "Jockey3"          # the card id the driver registers


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


XRUN_RE = re.compile(r"^\s*xruns\s*:\s*(\d+)", re.M)
STATE_RE = re.compile(r"^\s*state\s*:\s*(\S+)", re.M)


def pcm_status(index, pcm="pcm0p", sub="sub0"):
    """Parse a substream's status file.

    Returns {} when the substream is closed -- the status file exists but
    reports 'closed', which is not an error condition.
    """
    path = f"/proc/asound/card{index}/{pcm}/{sub}/status"
    text = _read(path)
    if not text or text.startswith("closed"):
        return {}
    out = {}
    m = XRUN_RE.search(text)
    if m:
        out["xruns"] = int(m.group(1))
    m = STATE_RE.search(text)
    if m:
        out["state"] = m.group(1)
    return out


def xruns(index, pcm="pcm0p", sub="sub0"):
    return pcm_status(index, pcm, sub).get("xruns", 0)


def rawmidi_device(index):
    for p in sorted(glob.glob(f"/proc/asound/card{index}/midi*")):
        name = os.path.basename(p)
        m = re.match(r"midi(\d+)", name)
        if m:
            return f"hw:{index},{m.group(1)}"
    return None


def have(tool):
    from shutil import which
    return which(tool) is not None


def missing_tools(tools):
    return [t for t in tools if not have(t)]


def stop_sound_server():
    """Release the device from PipeWire/PulseAudio.

    Without this the card is opened by the session's sound server and every
    exclusive-access test fails for a reason that has nothing to do with the
    driver. Returns what was stopped so it can be restarted afterwards.
    """
    stopped = []
    for unit in ("wireplumber.service", "pipewire.service",
                 "pipewire-pulse.service"):
        r = subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit],
                           capture_output=True)
        if r.returncode == 0:
            subprocess.run(["systemctl", "--user", "stop", unit],
                           capture_output=True)
            stopped.append(unit)
    return stopped


def start_sound_server(units):
    for unit in units:
        subprocess.run(["systemctl", "--user", "start", unit],
                       capture_output=True)
