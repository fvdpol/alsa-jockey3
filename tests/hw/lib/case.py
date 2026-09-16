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
import select
import subprocess
import sys
import time

# Where an operator asks a long run to wind up. Env var first, matching how
# capabilities.py finds its own machine-local file.
#
# The problem it solves: a case given a large iterations_per_run can hold the
# runner for hours, and there was no way to end it and keep what it had
# measured. Ctrl-C kills the child and re-raises, so the run never reaches
# results.write() and the whole batch is lost; the only alternative was to pull
# the device's power and let the case fail, which stores the data but records a
# driver failure that never happened. Neither is acceptable for a run that has
# already produced hours of good cycles.
STOP_FILE = os.environ.get("JOCKEY3_STOP_FILE",
                           os.path.expanduser("~/.config/jockey3/stop"))

def stop_requested_globally():
    """Has the operator asked the run to wind up? For the runner's own loop.

    The same signal Case.stop_requested() reads, without a Case instance --
    the runner uses it to stop starting new cases, while a case uses the
    method to stop starting new iterations. Both matter: a profile of many
    short cases needs the first, and one case with a large
    iterations_per_run needs the second.
    """
    try:
        return os.path.exists(STOP_FILE)
    except OSError:
        return False


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
        # Whether anybody is at the keyboard. Set by the runner from its own
        # --unattended flag, so a case never has to guess whether asking a
        # question would be answered or would simply hang.
        self.attended = os.environ.get("JT_ATTENDED") == "1"
        self.metrics = {}
        self._note = []
        self._stopped = False
        self._failures = []

    # ------------------------------------------------------------- recording

    def metric(self, name, value):
        self.metrics[name] = value

    def add(self, name, value=1):
        self.metrics[name] = self.metrics.get(name, 0) + value

    def note(self, text):
        self._note.append(str(text))

    def stop_requested(self):
        """Has the operator asked this run to wind up early?

        True once STOP_FILE exists. A case that loops should check this at the
        top of each iteration and `break` -- not exit. Breaking runs the case's
        normal verdict logic over the iterations it did complete, which is the
        whole point: those cycles are real measurements and stay real. A run
        stopped this way is a pass if what it managed passed.

        Says so once, in the log and in the metrics, so a short run is never
        mistaken later for a full one that happened to be configured small.
        """
        if self._stopped:
            return True
        try:
            if not os.path.exists(STOP_FILE):
                return False
        except OSError:
            return False
        self._stopped = True
        self.progress(f"    stop requested ({STOP_FILE}) -- winding up")
        self.metric("stopped_early", True)
        return True

    # ------------------------------------------------------- the operator

    def instruct(self, text):
        """Tell the operator to do something. No answer expected."""
        self.progress(f">> {text}")

    def ask(self, question, choices=("y", "n"), timeout=120):
        """Ask the operator something, and return the letter they chose.

        The prompt goes to stderr and the answer is read from stdin, which the
        runner leaves connected to the terminal. Everything said here is
        therefore captured in stderr.txt along with the rest of the case's
        account of itself -- one channel, one transcript.

        The prompt MUST end in a newline, and does. The runner streams this
        channel with readline(), so a prompt written without one is held in
        the pipe forever and the operator waits for a question that never
        appears while the case waits for an answer to a question never asked.

        Returns None on timeout or end of input. The timeout is the case's
        own and is deliberately far shorter than the runner's hour: an
        operator who walked away should cost a case, not an evening.
        """
        if not self.attended:
            return None
        opts = "/".join(choices)
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                self.progress(f"   (no answer within {timeout}s)")
                return None
            print(f"?? {question} [{opts}]", file=sys.stderr, flush=True)
            ready, _w, _x = select.select([sys.stdin], [], [], left)
            if not ready:
                continue
            line = sys.stdin.readline()
            if not line:
                self.progress("   (end of input -- nobody is there)")
                return None
            answer = line.strip().lower()
            if answer in choices:
                # Into the record, not just the terminal. A verdict a person
                # gave is evidence, and a month later "n" against "did the
                # LEDs blink" is the whole finding.
                self.note(f"{question} -> {answer}")
                return answer
            print(f"   answer one of: {opts}", file=sys.stderr, flush=True)

    def confirm(self, question, timeout=120):
        """Yes or no. Returns True, False, or None if nobody answered."""
        answer = self.ask(question, ("y", "n"), timeout)
        return None if answer is None else answer == "y"

    def progress(self, text):
        """Say what is happening, now, while it happens.

        Goes to stderr and is flushed immediately: the runner streams this
        channel straight to the terminal, so a case that takes eighty seconds
        is not eighty seconds of silence. Everything is still captured to
        stderr.txt, so nothing is lost by watching or by not watching.

        For humans, not for records -- a metric belongs in metric(), and the
        runner derives a failure reason from the LAST line of stderr, so
        progress can be as chatty as it likes without becoming the verdict.
        """
        print(text, file=sys.stderr, flush=True)

    def status(self, text):
        """A line the case is about to replace -- a countdown, a tally.

        Ends in a carriage return rather than a newline, which the runner
        reads as "transient" and redraws in place on a terminal. Anywhere
        else -- a pipe, a log, CI -- each update becomes an ordinary line,
        because a log that overwrote itself is a log that lost its history.

        Use progress() for anything that should still be there afterwards.
        """
        print(text, end="\r", file=sys.stderr, flush=True)

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
