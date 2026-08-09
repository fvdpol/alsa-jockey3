# SPDX-License-Identifier: GPL-2.0-or-later
"""Capture and classify kernel messages around a test case.

Slicing dmesg by timestamp is unreliable -- clock sources differ, and a case
that takes 3 ms can miss its own messages. Instead a unique marker is written
into /dev/kmsg before the case starts, and the log is sliced from the last
occurrence of that marker. The marker is in the log, so the boundary is exactly
where the case began, whatever the clock did.

Classification produces four buckets. The fourth is the one that earns its
keep: rather than choosing between an ever-growing allowlist and silently
discarding unknown messages, anything unrecognized is surfaced for a human to
look at once and then either allow or act on.
"""

import os
import re
import subprocess
import uuid

KMSG = "/dev/kmsg"
MARKER_PREFIX = "JT-MARK"

# Buckets
EXPECTED = "expected"        # ours, and normal for this case
UNEXPECTED = "unexpected"    # ours, and not normal here -> fail
UNRELATED = "unrelated"      # someone else's, known noise -> ignore
UNCLASSIFIED = "unclassified"  # -> flag for review
INVESTIGATE = "investigate"  # a defect, not a test failure -> abort

# Recognizing our own messages. dmesg renders the module name with either
# spelling depending on how it was loaded, so accept both. "ploytec" also
# counts as ours: when the driver is built with
# CONFIG_SND_USB_JOCKEY3_CODEC_KUNIT_TEST the codec suite runs at every module
# load and its TAP output carries the suite name rather than the module name.
OURS = re.compile(r"snd[-_]reloop[-_]jockey3|ploytec")


class Marker:
    """A boundary written into the kernel log."""

    def __init__(self, label):
        self.label = label
        self.token = f"{MARKER_PREFIX} {label} {uuid.uuid4().hex[:12]}"
        self.written = False

    def write(self):
        try:
            with open(KMSG, "w", encoding="utf-8") as f:
                f.write(self.token + "\n")
            self.written = True
        except OSError:
            # Not fatal: without a marker we fall back to capturing the whole
            # log, which is noisier but not wrong. Needs root.
            self.written = False
        return self.written


def read_log():
    try:
        out = subprocess.run(["dmesg", "--color=never"], capture_output=True,
                             text=True, timeout=30)
        return out.stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


def slice_since(lines, marker):
    if not marker or not marker.written:
        return lines
    idx = None
    for i, line in enumerate(lines):
        if marker.token in line:
            idx = i
    return lines[idx + 1:] if idx is not None else lines


def _compile(rules, key):
    out = []
    for entry in rules.get(key) or []:
        out.append((re.compile(entry["pattern"]), entry))
    return out


class Classifier:
    def __init__(self, rules):
        self.benign = _compile(rules, "benign")
        self.driver_fail = _compile(rules, "driver_fail")
        self.investigate = _compile(rules, "investigate")
        self.unrelated = _compile(rules, "unrelated")

    def classify(self, lines, expect_patterns=None):
        """Sort lines into buckets and extract metrics.

        expect_patterns are the case's own `expect_dmesg` entries: messages
        that are normal *for this case* and would be suspicious elsewhere.
        """
        expect = [re.compile(p) for p in (expect_patterns or [])]
        buckets = {EXPECTED: [], UNEXPECTED: [], UNRELATED: [],
                   UNCLASSIFIED: [], INVESTIGATE: []}
        metrics = {}

        for line in lines:
            if MARKER_PREFIX in line:
                continue

            # Defects first: an oops inside an otherwise expected message is
            # still an oops, and ordering here is a correctness property.
            hit = self._first(self.investigate, line)
            if hit:
                buckets[INVESTIGATE].append(line)
                continue

            ours = bool(OURS.search(line))

            if ours:
                hit = self._first(self.driver_fail, line)
                if hit:
                    buckets[UNEXPECTED].append(line)
                    continue

                rx, entry = self._first_pair(self.benign, line)
                if entry:
                    self._record(metrics, rx, entry, line)
                    # Benign messages are never failures on their own -- a
                    # stall can occur during any case, and failing on its
                    # presence would fail good builds. What matters is the
                    # count, so it becomes a metric and the case decides. A
                    # rate case fails when stalls do not recover; a MIDI case
                    # simply records that one happened.
                    buckets[EXPECTED].append(line)
                    continue

                if any(e.search(line) for e in expect):
                    buckets[EXPECTED].append(line)
                else:
                    buckets[UNEXPECTED].append(line)
                continue

            if self._first(self.unrelated, line):
                buckets[UNRELATED].append(line)
                continue

            buckets[UNCLASSIFIED].append(line)

        return buckets, metrics

    @staticmethod
    def _first(compiled, line):
        for rx, entry in compiled:
            if rx.search(line):
                return entry
        return None

    @staticmethod
    def _first_pair(compiled, line):
        for rx, entry in compiled:
            if rx.search(line):
                return rx, entry
        return None, None

    @staticmethod
    def _record(metrics, rx, entry, line):
        name = entry.get("metric")
        if not name:
            return
        group = entry.get("capture")
        if group:
            m = rx.search(line)
            if m:
                try:
                    metrics.setdefault(name, []).append(int(m.group(group)))
                except (ValueError, IndexError):
                    pass
            return
        metrics[name] = metrics.get(name, 0) + 1


def summarize(buckets):
    return {k: len(v) for k, v in buckets.items()}


def histogram(values):
    """Compact summary of a metric series.

    Mirrors what histogram_of_rate_change_delays.sh produced by hand, but
    keeps the distribution rather than reducing it to a mean -- the shape is
    the signal. A bimodal reset-delay distribution says something a mean does
    not.
    """
    if not values:
        return None
    vs = sorted(values)
    n = len(vs)
    counts = {}
    for v in vs:
        counts[v] = counts.get(v, 0) + 1
    return {
        "n": n,
        "min": vs[0],
        "max": vs[-1],
        "median": vs[n // 2],
        "mean": round(sum(vs) / n, 1),
        "counts": counts,
    }
