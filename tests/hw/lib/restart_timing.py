# SPDX-License-Identifier: GPL-2.0-or-later
"""Accumulate URB-restart timing from hardware runs into one growing dataset.

The driver logs, at dev_dbg level, how long each URB (re)start took to reach a
usable stream:

    Playback confirmed alive after 12 ms          <- a cold start
    Capture confirmed streaming after 48 ms       <- a warm restart

"alive" is jockey3_wait_urb_stream_started(require_healthy=False): the cold
paths -- first open, a rate change, a USB reset. "streaming" is
require_healthy=True: the stall watchdog's own warm restart, gated on
jockey3_stream_streaming_healthy(). The word in the message is the driver's own
classification, so no pairing or inference is needed.

This module turns those lines, across many runs, into per-run histograms keyed
by (stream, start_type) and stores them in data/restart_timing.json alongside
the run's identifying metadata. What is kept is deliberately small -- a count
per whole-millisecond bin, not the raw samples -- but enough to compute a
median or any percentile band later, and split by architecture, stream, start
type, kernel or dynamic-debug state without re-reading the run folders.

Two constraints worth stating up front:

  - The measurement *is* a dev_dbg line, so a run with dynamic debug off
    contributes nothing. Every sample here is a "dyndbg on" sample. That makes
    the numbers a conservative bound for production, where the same paths run
    faster -- which is the safe direction for sizing a grace period.
  - Debug kernels (KASAN, lockdep) inflate everything. Runs whose kernel
    carries a heavy debug option are skipped; the reason is recorded rather
    than silently dropped.
"""

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DATASET = os.path.join(HERE, os.pardir, "data", "restart_timing.json")

# Bump when the extraction below changes in a way that would give a different
# result from the same dmesg.txt. `restart_timing.py rebuild --reparse` then
# re-reads every source run folder that still exists.
EXTRACTOR_VERSION = 1

_CONFIRM = re.compile(
    r"(Playback|Capture) confirmed (alive|streaming) after (\d+) ms")
_WAIT = re.compile(
    r"Waiting up to (\d+) ms for (Playback|Capture) to (show liveness|stream steadily)")

_VERB_START = {"alive": "cold", "streaming": "warm"}
_MODE_START = {"show liveness": "cold", "stream steadily": "warm"}

# From env.HEAVY_DEBUG_OPTIONS; duplicated rather than imported so this module
# stays usable on its own. Keep in step with lib/env.py.
_HEAVY_DEBUG = {"KASAN", "KASAN_GENERIC", "PROVE_LOCKING", "LOCKDEP",
                "DEBUG_PAGEALLOC", "KMEMLEAK", "DEBUG_OBJECTS"}


def extract(dmesg_text):
    """Pull restart timings out of one run's dmesg.txt.

    Returns (hist, grace_seen):
      hist       {"playback|cold": {ms: count, ...}, "capture|warm": {...}, ...}
      grace_seen {"cold": [ms, ...], "warm": [ms, ...]}  -- the grace ceilings
                 that were in effect, so a censored tail (samples cannot exceed
                 the grace) is visible later.
    """
    hist = {}
    grace = {"cold": set(), "warm": set()}

    for line in dmesg_text.splitlines():
        m = _WAIT.search(line)
        if m:
            grace[_MODE_START[m.group(3)]].add(int(m.group(1)))
            continue
        m = _CONFIRM.search(line)
        if m:
            stream = m.group(1).lower()
            start = _VERB_START[m.group(2)]
            ms = int(m.group(3))
            key = f"{stream}|{start}"
            hist.setdefault(key, {})
            hist[key][ms] = hist[key].get(ms, 0) + 1

    return hist, {k: sorted(v) for k, v in grace.items()}


def kernel_is_prod(debug_options):
    """True if this kernel carries no heavy debug option (KASAN, lockdep, ...)."""
    return not (set(debug_options or []) & _HEAVY_DEBUG)


def source_from_run(run_json, dmesg_text):
    """Build a dataset source record from a run's run.json and dmesg.txt.

    Returns (record, reason). record is None when the run should not be
    ingested, and reason says why.
    """
    env = run_json.get("env", {})
    kern = env.get("kernel", {})
    debug_options = kern.get("debug_options") or []

    if not kernel_is_prod(debug_options):
        return None, "debug kernel (%s)" % ", ".join(
            o for o in debug_options if o in _HEAVY_DEBUG)

    hist, grace = extract(dmesg_text)
    if not hist:
        return None, "no restart-timing lines (dynamic debug was off?)"

    drv = env.get("driver", {})
    # "<target>/<dir>" under results/, so rebuild --reparse can find the folder
    # again. Derived from the run id, which is "<target>-<dir>".
    target = env.get("detected_target") or ""
    run_id = run_json.get("run_id") or ""
    run_path = (target + "/" + run_id[len(target) + 1:]) if (
        target and run_id.startswith(target + "-")) else run_id

    return {
        "run_id": run_id,
        "run_path": run_path,
        "target": env.get("detected_target"),
        "arch": kern.get("arch"),
        "case": ",".join(sorted({r["id"] for r in run_json.get("results", [])})),
        "kernel_release": kern.get("release"),
        "kernel_debug_options": debug_options,
        "driver_git": (drv.get("build") or {}).get("git_describe"),
        "driver_build_id": drv.get("build_id"),
        # Always "on" today -- the lines are dev_dbg. Recorded so a future
        # always-on measurement path can be told apart.
        "dyndbg": "on",
        "extractor_version": EXTRACTOR_VERSION,
        "hist": hist,
        "grace_ms": grace,
    }, "ok"


# --- dataset io -----------------------------------------------------------

def load(path=DATASET):
    if not os.path.exists(path):
        return {"version": 1, "extractor_version": EXTRACTOR_VERSION,
                "sources": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save(data, path=DATASET):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data["extractor_version"] = EXTRACTOR_VERSION
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def has_run(data, run_id):
    return any(s["run_id"] == run_id for s in data["sources"])


def add_source(data, record):
    """Insert or replace a source record. Returns True if the dataset changed."""
    for i, s in enumerate(data["sources"]):
        if s["run_id"] == record["run_id"]:
            if s == record:
                return False
            data["sources"][i] = record
            return True
    data["sources"].append(record)
    data["sources"].sort(key=lambda s: s["run_id"])
    return True


# --- aggregation --------------------------------------------------------

def aggregate(data, dims=("arch", "stream", "start_type"), where=None):
    """Sum per-source histograms into buckets keyed by the requested dims.

    dims is any ordered subset of: arch, stream, start_type, dyndbg,
    kernel_release, target, driver_git. `where` is an optional
    {dim: value-or-set} filter applied per source (arch/dyndbg/... ) before
    binning. Returns {tuple(dim values): {ms: count}}.
    """
    out = {}
    for s in data["sources"]:
        base = {
            "arch": s.get("arch"),
            "dyndbg": s.get("dyndbg"),
            "kernel_release": s.get("kernel_release"),
            "target": s.get("target"),
            "driver_git": s.get("driver_git"),
        }
        if where and not _matches(base, where):
            continue
        for key, bins in s["hist"].items():
            stream, start_type = key.split("|")
            row = dict(base, stream=stream, start_type=start_type)
            k = tuple(row.get(d) for d in dims)
            acc = out.setdefault(k, {})
            for ms, n in bins.items():
                acc[int(ms)] = acc.get(int(ms), 0) + n
    return out


def _matches(row, where):
    for dim, want in where.items():
        got = row.get(dim)
        if isinstance(want, (set, list, tuple)):
            if got not in want:
                return False
        elif got != want:
            return False
    return True


def grace_ceilings(data, start_type, where=None):
    """Every grace ceiling that was in effect for a start type, across sources."""
    seen = set()
    for s in data["sources"]:
        base = {"arch": s.get("arch"), "dyndbg": s.get("dyndbg"),
                "target": s.get("target"), "driver_git": s.get("driver_git"),
                "kernel_release": s.get("kernel_release")}
        if where and not _matches(base, where):
            continue
        seen.update(s.get("grace_ms", {}).get(start_type, []))
    return sorted(seen)


def pct_label(p):
    """Column label for a percentile: 'p90', 'p99.9' -- no trailing '.0'."""
    p = float(p)
    return "p%g" % p if p != int(p) else "p%d" % int(p)


def stats(bins, percentiles=(50, 90, 95, 99)):
    """Summary of one {ms: count} histogram. Percentiles may be fractional
    (99.9); each lands under pct_label(p)."""
    if not bins:
        return {"n": 0}
    items = sorted((int(ms), n) for ms, n in bins.items())
    total = sum(n for _, n in items)
    out = {"n": total, "min": items[0][0], "max": items[-1][0]}
    for p in percentiles:
        # nearest-rank on the cumulative count
        target = float(p) / 100 * total
        seen = 0
        for ms, n in items:
            seen += n
            if seen >= target:
                out[pct_label(p)] = ms
                break
    return out


def tail_is_censored(bins, ceilings, within=0.8):
    """True if the histogram's max sits close to the tightest grace ceiling,
    i.e. slower restarts were cut off and the real tail is unknown."""
    if not bins or not ceilings:
        return False
    return max(int(ms) for ms in bins) >= within * min(ceilings)
