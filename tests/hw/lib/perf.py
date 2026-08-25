# SPDX-License-Identifier: GPL-2.0-or-later
"""Host CPU/interrupt/tracing sampling for JT-PERF-001 (re/streaming_overhead.md).

Reads /proc/interrupts and /proc/stat directly rather than shelling out to
mpstat -- neither is installed on the project's test rigs, and the same
`/proc` parsing philosophy already used throughout lib/alsa.py applies here:
the numbers are what a case asserts on, and shelling out to a tool that may
not be there is fragile in a way that quietly reports nothing.

ns_per_callback() needs write access to tracefs (function_graph tracer,
restricted to the two URB completion handlers), which only the root-owned
`trace-callbacks` verb in tests/hw/priv/jockey3-testctl may touch -- see
lib/priv.py.
"""

import re

_STAT_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq")


def read_hcd_irq_total(hcd_name):
    """Sum of /proc/interrupts entries for the named HCD driver, across CPUs.

    Returns None if no line names this driver -- e.g. it is not the last
    field on every distro's /proc/interrupts (dwc2 sometimes appends a
    description), so the match is "driver name is one of the whitespace
    tokens", not "driver name is the last column".
    """
    if not hcd_name:
        return None
    total = 0
    found = False
    try:
        with open("/proc/interrupts", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None
    if not lines:
        return None
    ncols = len(lines[0].split())
    for line in lines[1:]:
        parts = line.split()
        if not parts:
            continue
        if hcd_name not in parts[ncols:] and hcd_name not in parts:
            continue
        # parts[0] is "NNN:"; the per-CPU counts follow, one column per CPU
        # header on the first line, everything after is description text.
        counts = parts[1:1 + ncols - 1]
        for c in counts:
            if c.isdigit():
                total += int(c)
                found = True
    return total if found else None


def read_cpu_stat():
    """Return the aggregate 'cpu' line of /proc/stat as a dict of jiffies."""
    try:
        with open("/proc/stat", encoding="utf-8") as f:
            first = f.readline()
    except OSError:
        return None
    parts = first.split()
    if not parts or parts[0] != "cpu":
        return None
    values = [int(v) for v in parts[1:]]
    return dict(zip(_STAT_FIELDS, values))


def cpu_pct_sys_irq_soft(before, after):
    """% of wall time spent in system+irq+softirq between two read_cpu_stat() samples.

    Deliberately excludes iowait, user and nice -- this is a measure of what
    the driver and the host controller cost the CPU, not of the machine's
    overall business.
    """
    if not before or not after:
        return None
    total_before = sum(before.get(k, 0) for k in _STAT_FIELDS)
    total_after = sum(after.get(k, 0) for k in _STAT_FIELDS)
    total_delta = total_after - total_before
    if total_delta <= 0:
        return None
    busy_delta = sum(
        after.get(k, 0) - before.get(k, 0) for k in ("system", "irq", "softirq"))
    return round(100.0 * busy_delta / total_delta, 2)


# --- function_graph trace parsing -------------------------------------

# Collapsed leaf form, no traced children:
#   " 3)   0.653 us    |  jockey3_playback_callback();"
_LEAF_RE = re.compile(
    r'^\s*(\d+)\)\s+([\d.]+)\s*(us|ms)\s*[+!]?\s*\|\s+(\w+)\(\);\s*$')

# Entry of a nested call:
#   " 3)               |  jockey3_playback_callback() {"
_ENTRY_RE = re.compile(r'^\s*(\d+)\)\s*\|?\s*(\w+)\(\)\s*\{\s*$')

# Close of a nested call, duration on the closing brace:
#   " 3)   1.204 us    |  }"
_EXIT_RE = re.compile(r'^\s*(\d+)\)\s+([\d.]+)\s*(us|ms)\s*[+!]?\s*\|\s*\}\s*$')

# Any other line with a "N)" CPU column, to track nesting depth per CPU
# without needing to understand what the line is (spinlocks, ktime_get,
# whatever the callback's real children turn out to be).
_OPEN_RE = re.compile(r'^\s*(\d+)\).*\{\s*$')
_CLOSE_RE = re.compile(r'^\s*(\d+)\).*\}\s*$')


def _to_ns(value, unit):
    return value * (1000.0 if unit == "us" else 1_000_000.0)


def parse_function_graph(text, function_names):
    """Extract per-call durations (ns) for the named functions.

    Handles both the collapsed single-line form (no traced children -- the
    common case when only these two functions are in set_ftrace_filter) and
    the nested entry/exit form, by tracking a per-CPU depth stack and
    attributing the exit duration to whichever of function_names opened the
    frame it closes.

    Returns {name: [ns, ns, ...]}. Malformed or unrecognised lines are
    ignored, not fatal -- ftrace's own header/comment lines and any format
    this parser does not know about are expected and silently skipped, and
    the count of durations actually recovered is the caller's signal that
    parsing worked, not a separate flag.
    """
    names = set(function_names)
    out = {name: [] for name in names}
    # Per-CPU stack of (function_name_or_None, is_target).
    stacks = {}

    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue

        m = _LEAF_RE.match(line)
        if m:
            cpu, num, unit, fn = m.groups()
            if fn in names:
                out[fn].append(_to_ns(float(num), unit))
            continue

        m = _ENTRY_RE.match(line)
        if m:
            cpu, fn = m.groups()
            stacks.setdefault(cpu, []).append((fn, fn in names))
            continue

        m = _EXIT_RE.match(line)
        if m:
            cpu, num, unit = m.groups()
            stack = stacks.get(cpu)
            if stack:
                fn, is_target = stack.pop()
                if is_target:
                    out[fn].append(_to_ns(float(num), unit))
            continue

        # Neither a leaf nor a line naming one of our functions -- still
        # track depth so a later exit line pops the right frame, otherwise
        # an unrelated child's "{"/"}" would desynchronize the stack.
        m = _OPEN_RE.match(line)
        if m:
            cpu = m.group(1)
            stacks.setdefault(cpu, []).append((None, False))
            continue
        m = _CLOSE_RE.match(line)
        if m:
            cpu = m.group(1)
            stack = stacks.get(cpu)
            if stack:
                stack.pop()
            continue

    return out


def mean(values):
    return sum(values) / len(values) if values else None
