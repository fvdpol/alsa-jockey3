# SPDX-License-Identifier: GPL-2.0-or-later
"""Result records for a test run.

One run produces one directory:

    <results-root>/<target>/<UTC-stamp>-<profile>/
        run.json        the record -- environment, results, metrics
        dmesg.txt       full kernel log captured across the run
        cases/          per-case stdout, stderr and artifacts

run.json is the thing that is kept and aggregated. Everything beside it is
evidence: useful while investigating, disposable afterwards, and never in git.
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict

# Case outcomes. SKIP and BLOCKED are distinct on purpose: SKIP means the
# profile said not to run it here, BLOCKED means we wanted to and could not
# (missing capability, missing tool). Only one of those is a coverage gap you
# might want to close.
PASS = "pass"
FAIL = "fail"
SKIP = "skip"
BLOCKED = "blocked"
ERROR = "error"
PENDING = "pending"          # manual case, awaiting a human answer

# Run outcomes.
RUN_PASS = "pass"
RUN_FAIL = "fail"
RUN_INVESTIGATE = "investigate"


@dataclass
class CaseResult:
    id: str
    iteration: int = 1
    status: str = PENDING
    mode: str = "automated"
    level: str = ""
    area: str = ""
    started: str = ""
    duration_s: float = 0.0
    params: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    dmesg: dict = field(default_factory=dict)
    note: str = ""
    reason: str = ""             # why skipped or blocked

    def as_dict(self):
        return asdict(self)


@dataclass
class Run:
    run_id: str
    profile: str
    target: str
    started: str
    operator: str = ""
    ended: str = ""
    outcome: str = PENDING
    env: dict = field(default_factory=dict)
    results: list = field(default_factory=list)
    unclassified: list = field(default_factory=list)

    def add(self, result: CaseResult):
        self.results.append(result)

    def counts(self):
        c = {}
        for r in self.results:
            c[r.status] = c.get(r.status, 0) + 1
        return c

    def as_dict(self):
        d = asdict(self)
        d["results"] = [r.as_dict() if isinstance(r, CaseResult) else r
                        for r in self.results]
        d["counts"] = self.counts()
        return d


def utc_stamp(t=None):
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t))


def utc_iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def results_root():
    """Where run directories live.

    Defaults into the Seafile tree so the test machines share results without
    each needing a git checkout. Override with JOCKEY3_RESULTS_DIR.
    """
    env = os.environ.get("JOCKEY3_RESULTS_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "results"))


def run_dir(root, target, profile, stamp):
    return os.path.join(root, target, f"{stamp}-{profile}")


def write(run: Run, path):
    """Write run.json atomically.

    A soak run can be interrupted by the very crash it was looking for, and a
    truncated result file loses the evidence of everything that came before.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(run.as_dict(), f, indent=2, sort_keys=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
