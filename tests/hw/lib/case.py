# SPDX-License-Identifier: GPL-2.0-or-later
"""The case side of the runner contract.

A case is an ordinary executable. It reads its configuration from the
environment, does its work, records metrics, and exits with a status code.
It does NOT inspect the kernel log -- classification is the runner's job,
because a case cannot be trusted to notice an oops it caused.

    from lib.case import Case

    c = Case()
    for rate in c.params.get("rates", [44100]):
        ...
        c.metric("xruns", n)
    c.done()
"""

import json
import os
import subprocess
import sys

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_SKIP = 2
EXIT_BLOCKED = 3


class Case:
    def __init__(self):
        self.id = os.environ.get("JT_CASE_ID", "unknown")
        self.iteration = int(os.environ.get("JT_ITERATION", "1"))
        self.params = json.loads(os.environ.get("JT_PARAMS") or "{}")
        card = os.environ.get("JT_CARD", "")
        self.card = int(card) if card.isdigit() else None
        self.device = os.environ.get("JT_DEVICE") or None
        self.workdir = os.environ.get("JT_WORKDIR") or "."
        self.repo = os.environ.get("JT_REPO") or os.getcwd()
        self.result_file = os.environ.get("JT_RESULT_FILE")
        self.metrics = {}
        self._note = []
        self._failures = []

    # ------------------------------------------------------------- recording

    def metric(self, name, value):
        self.metrics[name] = value

    def add(self, name, value=1):
        self.metrics[name] = self.metrics.get(name, 0) + value

    def note(self, text):
        self._note.append(str(text))

    def fail(self, text):
        """Record a failure but keep going.

        A case that stops at the first failure tells you the first rate that
        broke. A case that continues tells you whether it was one rate or all
        four, which is a different bug.
        """
        self._failures.append(str(text))
        print(f"FAIL: {text}", file=sys.stderr)

    # --------------------------------------------------------------- running

    def run(self, cmd, timeout=120, **kw):
        """Run a command, returning (rc, stdout, stderr)."""
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, **kw)
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {timeout}s"
        except OSError as e:
            return 125, "", str(e)

    def require_card(self):
        if self.card is None:
            self.blocked("no Jockey 3 card found")

    def require_tools(self, *tools):
        from shutil import which
        missing = [t for t in tools if which(t) is None]
        if missing:
            self.blocked("missing tools: " + ", ".join(missing))

    @property
    def failed(self):
        return bool(self._failures)

    # ----------------------------------------------------------------- exits

    def _write(self):
        if not self.result_file:
            return
        payload = {"metrics": self.metrics, "note": "; ".join(self._note)}
        try:
            with open(self.result_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.write("\n")
        except OSError:
            pass

    def done(self):
        """Exit pass unless something was recorded as failed."""
        self._write()
        if self._failures:
            print("; ".join(self._failures[:3]), file=sys.stderr)
            sys.exit(EXIT_FAIL)
        sys.exit(EXIT_PASS)

    def skip(self, reason):
        self.note(reason)
        self._write()
        print(reason, file=sys.stderr)
        sys.exit(EXIT_SKIP)

    def blocked(self, reason):
        self.note(reason)
        self._write()
        print(reason, file=sys.stderr)
        sys.exit(EXIT_BLOCKED)


def bootstrap():
    """Put tests/hw on sys.path so `from lib...` works from cases/."""
    here = os.path.dirname(os.path.abspath(__file__))
    hw = os.path.normpath(os.path.join(here, ".."))
    if hw not in sys.path:
        sys.path.insert(0, hw)
    return hw
