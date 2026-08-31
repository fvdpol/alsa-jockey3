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

The driver's dev_dbg() output is the exception to that. It is silent unless an
operator turns dynamic debug up, there is a lot of it, and none of it signals a
fault -- so it is classified on syslog priority (see msg_level()): a line that
is ours and KERN_DEBUG is a trace, no per-message rule required. That keeps the
allowlist from growing every time a dev_dbg() is added to the driver.
"""

import atexit
import os
import re
import signal
import subprocess
import uuid

from lib import priv

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
#
# The bare function name counts too. When the kernel rate-limits one of our
# dev_*_ratelimited() call sites it emits "<function>: N callbacks suppressed"
# with no device prefix and no module name, so a line that is unmistakably ours
# failed the ownership test -- and since a case's expect_dmesg entries are only
# consulted for messages that are ours, no expect rule could ever whitelist it.
# That is why "jockey3_urb_error_give_up: N callbacks suppressed" sat in the
# unclassified bucket of every run the suite has ever produced. There is now an
# explicit benign rule for it in rules.yaml (the "callbacks suppressed" one),
# because the summary line carries no syslog priority and so is not covered by
# the KERN_DEBUG shortcut below.
OURS = re.compile(r"snd[-_]reloop[-_]jockey3|ploytec|jockey3_\w+")

# dmesg --raw prefixes every line with its syslog priority as "<N>". The low
# three bits are the level; 7 is KERN_DEBUG. Every dev_dbg() in this driver --
# and every line dynamic debug turns on -- comes out at that level, and this
# driver reports real problems through dev_warn()/dev_err() (KERN_WARNING and
# below). So a line that is ours AND KERN_DEBUG is a trace by construction:
# classify() treats it as expected without needing a rule per message, which is
# what lets an operator run "dyndbg=+p" during an investigation without turning
# every case red. Falls back cleanly when the prefix is absent (a box where
# dmesg is unrestricted and read without --raw): level is None and the shortcut
# simply does not fire.
LEVEL_RE = re.compile(r"^<(\d+)>")
KERN_DEBUG = 7


def msg_level(raw):
    """Syslog level (0-7) from a dmesg --raw line, or None if unprefixed."""
    m = LEVEL_RE.match(raw)
    return int(m.group(1)) & 7 if m else None


# The label charset the privileged helper accepts. It validates the token it is
# handed -- a token carrying a space or a newline could forge a boundary or
# split a log line in two -- and rejects anything else with "malformed marker
# token". priv.dmesg_mark() then returns False, Marker.write() treats that as
# non-fatal, and the marker is simply absent.
#
# That silence is expensive. A JT-RATE-001 run used '@' in its labels to write
# "#change3@88200"; every start marker was rejected, every end marker (no '@')
# got through, and the windows each shifted by one change. The case then
# reported "capture stalled on 0/20 changes" alongside six resets, and looked
# like a clean run rather than a broken instrument.
#
# So labels are sanitized here, against the helper's own charset, rather than
# left to be rejected on the far side of a subprocess. Keep this in step with
# priv/jockey3-testctl.
LABEL_OK = re.compile(r"[^A-Za-z0-9._:#+-]")


class Marker:
    """A boundary written into the kernel log."""

    def __init__(self, label):
        self.label = LABEL_OK.sub("-", str(label))[:64]
        self.token = f"{MARKER_PREFIX} {self.label} {uuid.uuid4().hex[:12]}"
        self.written = False

    def write(self):
        # Direct write when we already have the privilege, helper otherwise.
        # Trying it first rather than always going through sudo keeps a
        # root-run suite working on a machine where the helper was never
        # installed.
        try:
            with open(KMSG, "w", encoding="utf-8") as f:
                f.write(self.token + "\n")
            self.written = True
            return self.written
        except OSError:
            pass
        # Not fatal if this fails too: without a marker we fall back to
        # capturing the whole log, which is noisier but not wrong.
        self.written = priv.dmesg_mark(self.token)
        return self.written


def read_log():
    """The kernel log, by whatever route is available.

    dmesg is readable unprivileged only when kernel.dmesg_restrict is 0, which
    it is not on the test machines, so this normally goes through the helper.
    Tried directly first so the function still works on a box where dmesg is
    unrestricted and no helper is installed.

    --raw keeps the "<N>" priority prefix the classifier wants, but it is
    mutually exclusive with --color on util-linux and rejected outright on some
    builds, so a plain read is tried next. Losing the prefix only disables the
    KERN_DEBUG shortcut; losing the log entirely would suppress every by-change
    figure a case reports, so the fallback matters.
    """
    for argv in (["dmesg", "--raw"], ["dmesg", "--color=never"]):
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            if out.returncode == 0 and out.stdout:
                return out.stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            pass
    return priv.dmesg_read()


def read_lines(path):
    """A saved kmsg capture as a list of lines, or None if it is absent/empty."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    return text.splitlines() if text.strip() else None


class KmsgCapture:
    """Stream the kernel log to a file for the whole run.

    read_log() reads the kernel ring buffer, which holds only a few MB. A long
    rate soak with dynamic debug on writes far more than that, so by the time
    the run ends most of it has scrolled out -- the same way the marker-loss
    incidents lost every by-change figure. This copies every record to
    <run>/kmsg.log as it is emitted, so nothing is lost to wrap. It matters
    most where memory is tight (the Pi 1): the child writes straight to the
    file descriptor, this side buffers nothing.

    Best-effort throughout. If the capture cannot start -- no privilege, an old
    helper without the verb -- the run falls back to read_log() and its
    dmesg.txt is bounded by the ring buffer as before.
    """

    def __init__(self, dest):
        self.dest = dest
        self._proc = None
        self._fh = None

    def start(self):
        argv = None
        if os.geteuid() == 0:
            argv = ["dmesg", "--follow", "--raw"]
        elif priv.available()[0]:
            argv = priv.verb_argv("kmsg-follow")
        if not argv:
            return False
        try:
            self._fh = open(self.dest, "w", encoding="utf-8")
            # stdin off the terminal and a fresh session: the follower runs
            # under `sudo`, whose use_pty (default in recent sudo) would
            # otherwise put the runner's terminal into raw mode to relay a
            # pty -- and if we then have to SIGKILL it, it never restores it,
            # leaving every later line staircased. With no controlling tty
            # there is nothing for sudo to grab.
            self._proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=self._fh,
                stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            self._cleanup_fh()
            return False
        atexit.register(self._kill)
        return True

    def stop(self):
        """Terminate the capture. Returns the path if it holds anything."""
        if self._proc is not None:
            self._kill()
        self._cleanup_fh()
        try:
            return self.dest if os.path.getsize(self.dest) > 0 else None
        except OSError:
            return None

    def _kill(self):
        p, self._proc = self._proc, None
        if p is None or p.poll() is not None:
            return
        # Signal the whole session (sudo -> helper -> dmesg), not just sudo,
        # so nothing is orphaned; fall back to the bare process if the group
        # is already gone.
        for sig, grace in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
            try:
                os.killpg(os.getpgid(p.pid), sig)
            except (ProcessLookupError, PermissionError):
                try:
                    p.send_signal(sig)
                except ProcessLookupError:
                    return
            try:
                p.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue

    def _cleanup_fh(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


def slice_since(lines, marker):
    if not marker or not marker.written:
        return lines
    idx = None
    for i, line in enumerate(lines):
        if marker.token in line:
            idx = i
    return lines[idx + 1:] if idx is not None else lines


def run_log(lines, marker):
    """This run's slice of the kernel log, plus a header describing it.

    Returns (text, trimmed). The ring buffer on the test machines holds
    something like eighteen hours under ordinary logging -- but that assumes
    log_buf_len=4M (see tests/README.md, "Test machine prerequisites"). At the
    default 128 KiB (CONFIG_LOG_BUF_SHIFT=17) a marker-heavy case wraps the
    buffer well before the run ends: a 20000-change JT-RATE-003 run writes
    40000 markers, and only the last ~1050 changes were still in dmesg by the
    time it read the log. That presented as "94% of markers missing" with
    every per-change figure suppressed -- indistinguishable, from this file's
    output alone, from the marker-charset-rejection failure mode LABEL_OK
    guards against above. Undersized log_buf_len is the one to check first;
    it explained both real occurrences of "markers missing" so far.

    Short of that, an untrimmed capture is mostly earlier runs, which is not
    merely untidy: two JT-RATE-001 runs on 2026-08-17 that stalled zero times
    in 244 changes shipped dmesg.txt files containing 62 and 10 "Capture URB
    has stalled." lines, every one of them from a previous run. Read raw,
    either file argues the opposite of what the run measured.

    When the marker is missing the whole log is kept rather than nothing --
    too much context can be filtered down later, too little cannot be
    recovered -- so the header has to distinguish the two cases loudly. A
    silently untrimmed file that looks trimmed is the failure this guards
    against.
    """
    kept = slice_since(lines, marker)
    trimmed = bool(marker and marker.written and len(kept) < len(lines))
    # read_log() runs `dmesg --raw` for the classifier's sake; the saved
    # artifact does not need the "<N>" priority prefix and reads better
    # without it, so it comes off here. The "[time] message" body is
    # unchanged from before --raw.
    kept = [LEVEL_RE.sub("", ln) for ln in kept]
    if trimmed:
        head = (f"# kernel log from the start of this run\n"
                f"# marker: {marker.token}\n"
                f"# {len(lines) - len(kept)} earlier line(s) trimmed\n")
    elif marker and marker.written and not lines:
        # The marker went to /dev/kmsg but the log came back empty, so it
        # could not be found to slice on. Almost always dmesg-read itself
        # failing -- an option the installed helper does not accept, or a
        # permission change -- not a marker problem. Named precisely because
        # the two have completely different fixes.
        head = ("# NO KERNEL LOG -- dmesg-read returned nothing, so this run's\n"
                "# messages could not be captured at all. The run-start marker\n"
                "# was written but there was no log to find it in. Check that\n"
                "# the privileged helper's dmesg-read verb still works on this\n"
                "# host; every by-change figure is suppressed until it does.\n")
    else:
        head = ("# WHOLE kernel ring buffer -- the run-start marker was not\n"
                "# written, so this file also covers earlier runs. Lines below\n"
                "# may predate this run entirely; check timestamps before\n"
                "# attributing anything here to it.\n")
    return head + "\n".join(kept) + "\n", trimmed


def _compile(rules, key):
    out = []
    for entry in rules.get(key) or []:
        out.append((re.compile(entry["pattern"]), entry))
    return out


# dmesg prefixes every line with a timestamp, and may prefix a facility level
# as well. Patterns are written against the message, so the prefix is stripped
# before matching.
#
# This is not cosmetic. Several rules are anchored -- '^\s*# Subtest: ploytec',
# '^\s*(KTAP|TAP) version \d' -- and against a real dmesg line those anchors
# can never match, because the line starts with "[ 5871.543119] ". The rules
# looked correct and classified nothing: the first hardware run produced 1602
# unclassified and 680 wrongly-unexpected lines, which failed every case that
# unloaded the module. The selftest did not catch it because its fixtures were
# bare message text, so the fixtures now carry timestamps too.
PREFIX_RE = re.compile(r'^(?:<\d+>)?\s*(?:\[\s*\d+\.\d+\]\s*)?')


def strip_prefix(line):
    return PREFIX_RE.sub("", line, count=1)


class Classifier:
    def __init__(self, rules):
        self.benign = _compile(rules, "benign")
        self.driver_fail = _compile(rules, "driver_fail")
        # Failures that identify themselves by the ALSA card rather than by
        # the module, so OURS never matches them and the driver_fail list
        # above is never consulted. Checked separately, and before the
        # ownership test, or they would be unclassifiable by construction.
        self.driver_fail_by_device = _compile(rules, "driver_fail_by_device")
        self.investigate = _compile(rules, "investigate")
        self.unrelated = _compile(rules, "unrelated")

    def classify(self, lines, expect_patterns=None):
        """Sort lines into buckets and extract metrics.

        expect_patterns are the case's own `expect_dmesg` entries: messages
        that are normal *for this case* and would be suspicious elsewhere.

        Matching is done on the message with the dmesg prefix removed; the
        buckets keep the original line, because a timestamp is most of what
        makes a reported message useful.
        """
        expect = [re.compile(p) for p in (expect_patterns or [])]
        buckets = {EXPECTED: [], UNEXPECTED: [], UNRELATED: [],
                   UNCLASSIFIED: [], INVESTIGATE: []}
        metrics = {}

        for raw in lines:
            if MARKER_PREFIX in raw:
                continue
            line = strip_prefix(raw)
            level = msg_level(raw)

            # Defects first: an oops inside an otherwise expected message is
            # still an oops, and ordering here is a correctness property.
            hit = self._first(self.investigate, line)
            if hit:
                buckets[INVESTIGATE].append(raw)
                continue

            # Ours by device rather than by name. Tested before ownership,
            # because ownership is exactly what these lines fail to state.
            hit = self._first(self.driver_fail_by_device, line)
            if hit:
                buckets[UNEXPECTED].append(raw)
                continue

            ours = bool(OURS.search(line))

            if ours:
                hit = self._first(self.driver_fail, line)
                if hit:
                    buckets[UNEXPECTED].append(raw)
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
                    buckets[EXPECTED].append(raw)
                    continue

                # A KERN_DEBUG line from our driver is a trace by construction
                # (see msg_level()). Checked after driver_fail and the benign
                # rules, so a debug line that carries a metric is still counted,
                # but before the per-case expect list, so no case has to
                # enumerate the driver's dev_dbg() output to run with dynamic
                # debug on. Defects were already handled by the investigate
                # pass above, which runs regardless of level.
                if level == KERN_DEBUG:
                    buckets[EXPECTED].append(raw)
                    continue

                if any(e.search(line) for e in expect):
                    buckets[EXPECTED].append(raw)
                else:
                    buckets[UNEXPECTED].append(raw)
                continue

            if self._first(self.unrelated, line):
                buckets[UNRELATED].append(raw)
                continue

            buckets[UNCLASSIFIED].append(raw)

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
