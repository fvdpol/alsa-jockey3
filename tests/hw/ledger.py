#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""What has been tested, on what, how recently -- and which way the numbers move.

    ./ledger.py                        coverage table
    ./ledger.py --target x86_64-debug  one target, and drop the target column
    ./ledger.py --metrics              metric trends per target
    ./ledger.py --markdown             as markdown for publishing
    ./ledger.py --matrix               one-glance pass/fail pivot, all targets

The question this answers is "am I looking at stale data?". A pass from three
weeks ago still means something; it just means less. Showing the age and the
distance in commits keeps old results useful without letting them masquerade
as current -- which is the failure mode of every dashboard that only shows a
green tick.

Every row carries the case's title, and in markdown its pass criterion as a
"validates" column. A table of bare case IDs answers "is JT-PCM-006 covered?"
only for a reader who already knows what JT-PCM-006 is -- which is nobody
being shown a status report, and not the author either, three months later.
The criterion is markdown-only because that is the form that gets published
and read away from the catalog; in a terminal it would push the dates off the
right edge, and the dates are what the table is for.

Build and component cases (L1/L2) are reported once, under the target
"(source)", rather than per target. They test the source: checkpatch does not
have a different opinion depending on which machine is plugged into the DJ
controller, and repeating an identical verdict under six targets says nothing
six times. Their skips on a hardware machine are discarded rather than treated
as the latest word -- a hardware run skips them by design, and letting that
stand reported "skip" for a gate that had passed on the build host an hour
earlier.

Commit distance is scoped to the driver's own source files -- the list
sync-driver.sh copies into a kernel tree -- not the whole repository.
Attributing relevance per individual file would be false precision in a
driver where nearly every change touches jockey3.c, but whole-repository
distance turned out to be too coarse in the other direction: this repository
also holds RE notes, documentation and the test framework, so a commit that
only published this ledger's own output moved HEAD and read as the driver
having changed. It is only computable where the git repository is, so it
reads "?" on a test machine, which has the results but no checkout.
"""

import argparse
import functools
import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import results, yamlio  # noqa: E402

if not yamlio.available():
    sys.exit("PyYAML is required: apt install python3-yaml")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))


def load(name):
    with open(os.path.join(HERE, name), "r", encoding="utf-8") as f:
        return yamlio.safe_load(f)


def load_runs(root):
    runs = []
    for path in sorted(glob.glob(os.path.join(root, "*", "*", "run.json"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["_path"] = path
            runs.append(data)
        except (OSError, ValueError):
            continue
    return runs


@functools.lru_cache(maxsize=None)
def driver_paths():
    """The exact source files a build actually compiles into the module.

    Read from `sync-driver.sh --list` -- the one file list this repository
    already maintains as the single point of truth for what ships, so a new
    source file added there is picked up here for free instead of needing a
    second list that can drift from the first.

    Cached for the process: this is called once per indexed run, and the
    list does not change mid-invocation.
    """
    try:
        p = subprocess.run(
            ["bash", os.path.join(REPO, "tests", "build", "sync-driver.sh"),
             "--list"],
            capture_output=True, text=True, timeout=15)
        paths = [line.partition(":")[0] for line in p.stdout.splitlines()
                 if line.strip()] if p.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        paths = []
    return tuple(paths)


def commits_since(git_hash):
    """How much driver source has changed since the revision that was tested.

    Scoped to the files sync-driver.sh actually copies into a kernel tree,
    not the whole repository. Whole-repository distance was tried first and
    turned out to be self-defeating: this repository also holds RE notes,
    documentation and the test framework, so a commit that only published
    this very matrix -- or edited a checkpatch rule, or added a paragraph to
    re/protocol_analysis.md -- moved HEAD and made every fresh result read
    stale on the next run, without a single line of driver source having
    changed. Scoping to the driver's own file list is coarser than per-file
    attribution (which is deliberately not done -- see the coverage table's
    docstring) while still answering the question that matters: did the
    thing that got tested change.
    """
    if not git_hash:
        return None
    cmd = ["git", "-C", REPO, "rev-list", "--count", f"{git_hash}..HEAD"]
    paths = driver_paths()
    if paths:
        cmd += ["--", *paths]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if p.returncode == 0:
            return int(p.stdout.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def age_days(iso):
    if not iso:
        return None
    try:
        t = time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None
    return int((time.time() - t) / 86400)


def driver_rev(git):
    """How to label the driver a run tested.

    git_describe carries a "-dirty" suffix taken from the whole repository,
    which is the wrong question. This repository holds the test framework as
    well as the driver, so editing a classification rule marks a build dirty
    even though the driver sources compiled into it were exactly the committed
    ones -- which is how a prod kernel came to be reported as "2f9ac17-dirty"
    when its driver was verbatim c543cf70efd1.

    kernel_driver_dirty answers the question actually being asked: were there
    uncommitted changes under sound/usb/jockey3 in the tree this was built
    from. Prefer it, and fall back to the repository-wide flag for manifests
    written before it existed.
    """
    rev = git.get("git_describe") or git.get("git_hash")
    if not rev:
        return None
    base = rev[:-len("-dirty")] if rev.endswith("-dirty") else rev
    if "kernel_driver_dirty" in git:
        return base + ("-dirty" if git["kernel_driver_dirty"] else "")
    return rev


def is_dirty(git):
    """Whether the driver under test had uncommitted changes.

    Prefers the driver-scoped flag over the repository-wide one, for the same
    reason driver_rev() does -- a scratch edit to a classification rule
    should not disqualify a run whose driver sources were exactly the
    committed ones. A manifest written before either flag existed carries
    neither key; that is treated as clean rather than excluded, since there
    is no evidence of dirt and older data should not vanish from the record
    on that account alone.
    """
    if "kernel_driver_dirty" in git:
        return bool(git["kernel_driver_dirty"])
    return bool(git.get("dirty"))


BUILD_LEVELS = ("L1", "L2")


def is_source_scoped(case):
    """Whether this case's result belongs under "(source)", not a target.

    L1/L2 usually means source-level: checkpatch and codec correctness don't
    have a different opinion depending on which machine is plugged into the
    DJ controller. A case can opt out with `per_target: true` in the catalog
    when that assumption doesn't hold -- JT-CODEC-005 is L2 (one component,
    no kernel running) but its numbers are a throughput benchmark, which is a
    property of the host, not the source, so it needs a real per-target
    column like an L3+ case would get.
    """
    return case.get("level") in BUILD_LEVELS and not case.get("per_target")


def build_index(runs, cases=None, clean_only=False):
    """Latest pass and latest attempt per (target, case).

    Build and component cases are indexed under the pseudo-target "*" as well
    as their own, because they are properties of the SOURCE, not of a target.
    checkpatch does not have a different opinion depending on which machine is
    plugged into the DJ controller.

    Their skips are dropped entirely. A hardware run skips them by design --
    that is not this machine's job -- and letting "skip" stand as the latest
    word made the coverage table report "skip 2026-08-10" for a gate that had
    actually passed on the build host an hour earlier.

    clean_only drops dirty runs from consideration as if they never happened,
    rather than just labelling them. Bench iterations and other dev-in-progress
    runs are real and worth keeping in the local record, but a status matrix
    published to the repository is a claim about committed code -- a pass
    against a scratch edit that never got committed has no meaning there.
    """
    cases = cases or {}
    idx = {}
    for run in runs:
        target = run.get("target", "?")
        git = ((run.get("env") or {}).get("driver") or {}).get("build") or {}
        if clean_only and is_dirty(git):
            continue
        rev = driver_rev(git)
        for r in run.get("results", []):
            when = run.get("started")
            entry = {"when": when, "rev": rev, "target": target,
                     "hash": git.get("git_hash"), "status": r["status"],
                     "run": run["_path"]}
            case = cases.get(r["id"]) or {}
            keys = [(target, r["id"])]
            if is_source_scoped(case):
                if r["status"] == results.SKIP:
                    continue
                keys.append(("*", r["id"]))
            for key in keys:
                slot = idx.setdefault(key, {"pass": None, "last": None})
                if slot["last"] is None or (when or "") > (slot["last"]["when"] or ""):
                    slot["last"] = entry
                if r["status"] == results.PASS:
                    if (slot["pass"] is None
                            or (when or "") > (slot["pass"]["when"] or "")):
                        slot["pass"] = entry
    return idx


def oneline(text, limit=None):
    """Collapse a folded YAML block to one line, optionally truncated."""
    s = " ".join(str(text or "").split())
    if limit and len(s) > limit:
        s = s[:limit - 1].rstrip() + "…"
    return s


TITLE_WIDTH = 44


def coverage(cases, targets, idx, markdown=False, single_target=False):
    """Which cases have passed where, and how long ago.

    Carries each case's title, and in markdown its pass criterion too. A table
    of bare IDs answers "is JT-PCM-006 covered" only for someone who already
    knows what JT-PCM-006 is -- which is nobody reading a status report, and
    not the author either three months later.

    The pass criterion is markdown-only because that is the form that gets
    published and read away from the catalog; on a terminal it would push the
    dates off the right edge, and the dates are what the table is for.
    """
    rows = []

    def row_for(cid, case, lookup, label):
        title = oneline(case.get("title"),
                        None if markdown else TITLE_WIDTH)
        slot = idx.get((lookup, cid))
        if slot and slot["pass"]:
            p = slot["pass"]
            n = commits_since(p.get("hash"))
            cells = [(p["when"] or "")[:10], p.get("rev") or "?",
                     "?" if n is None else str(n),
                     f"{age_days(p['when'])}d"]
        elif slot and slot["last"]:
            lst = slot["last"]
            cells = [f"{lst['status']} {(lst['when'] or '')[:10]}",
                     lst.get("rev") or "?", "-", "-"]
        else:
            # Never run here. This is the row the table exists for.
            cells = [("planned" if case["status"] != "implemented"
                      else "never run"), "-", "-", "-"]
        row = [cid, title, case["level"], case["mode"]] + cells
        if markdown:
            row.append(oneline(case.get("pass")) or "-")
        if not single_target:
            row.insert(0, label)
        return tuple(row)

    # Build and component cases once, not once per target: they test the
    # source, and repeating an identical checkpatch verdict under six targets
    # says nothing six times. Hardware cases are per target, where the target
    # is the whole point.
    for cid, case in cases.items():
        if is_source_scoped(case):
            rows.append(row_for(cid, case, "*", "(source)"))
    for tname in targets:
        for cid, case in cases.items():
            if not is_source_scoped(case):
                rows.append(row_for(cid, case, tname, tname))

    head = ["case", "title", "lvl", "mode", "last pass", "driver",
            "commits", "age"]
    if markdown:
        head.append("validates")
    if not single_target:
        head.insert(0, "target")
    head = tuple(head)

    if markdown:
        out = ["| " + " | ".join(head) + " |",
               "|" + "|".join(["---"] * len(head)) + "|"]
        # A pipe inside a cell would silently split the column.
        out += ["| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |"
                for r in rows]
        return "\n".join(out)

    widths = [max(len(str(r[i])) for r in ([head] + rows))
              for i in range(len(head))]
    lines = ["  ".join(h.ljust(w) for h, w in zip(head, widths))]
    lines.append("  ".join("-" * w for w in widths))
    lines += ["  ".join(str(c).ljust(w) for c, w in zip(r, widths))
              for r in rows]
    return "\n".join(lines)


MATRIX_LEGEND = """\
| | |
|---|---|
| ✅ | passed, against the current driver |
| 🟡 | passed, but the driver has moved on since -- may no longer hold |
| ❌ | failed, against the current driver |
| 🟠 | failed, but the driver has moved on since -- may already be fixed |
| ⬜ | skipped here by design (not applicable to this target) |
| 🚫 | blocked -- wanted to run, couldn't (missing capability or tool) |
| ⏳ | manual case, awaiting an operator's answer |
| – | not applicable (build/component cases run once, under "(source)") |
| (blank) | never run |
"""


def matrix_cell(slot):
    """One glyph for one (target, case): see MATRIX_LEGEND.

    "Fresh" means passed or failed against the exact commit HEAD is at now.
    Anything else -- a real commit gap, or an unknown one because this
    machine has no checkout to measure from -- is treated the same way: not
    proven current, so it does not get a plain green tick. commits_since()
    is whole-repository, the same measure the coverage table already uses,
    so a matrix and a coverage table run side by side never disagree about
    what counts as stale.
    """
    if not slot:
        return ""
    if slot["pass"]:
        n = commits_since(slot["pass"].get("hash"))
        return "✅" if n == 0 else "🟡"
    last = slot["last"]
    if last is None:
        return ""
    status = last["status"]
    if status == results.SKIP:
        return "⬜"
    if status == results.BLOCKED:
        return "🚫"
    if status == results.PENDING:
        return "⏳"
    n = commits_since(last.get("hash"))
    return "❌" if n == 0 else "🟠"


def matrix(cases, targets, idx):
    """Pass/fail pivot: cases on the rows, targets as columns.

    Built for one glance from a README, not for the detail the coverage
    table carries -- driver revision, commit count, age. Those stay in
    `coverage()`; this is the summary that sends a reader there.
    """
    columns = ["(source)"] + list(targets)
    head = ["Case", "Title"] + columns
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join(["---"] * len(head)) + "|"]
    for cid, case in cases.items():
        is_build = is_source_scoped(case)
        title = oneline(case.get("title"))
        row = [cid, title.replace("|", "\\|")]
        for col in columns:
            source_col = col == "(source)"
            if is_build != source_col:
                row.append("–")
            else:
                # Build/component results are indexed under the pseudo-target
                # "*" (see build_index), not the "(source)" column label.
                lookup = "*" if source_col else col
                row.append(matrix_cell(idx.get((lookup, cid))))
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


def metric_trends(runs, markdown=False):
    """Every numeric metric, per target, oldest to newest.

    A metric moving while every verdict stays green is exactly the signal that
    is invisible without this.
    """
    series = {}
    for run in sorted(runs, key=lambda r: r.get("started") or ""):
        target = run.get("target", "?")
        for r in run.get("results", []):
            for name, value in (r.get("metrics") or {}).items():
                if isinstance(value, dict):
                    value = value.get("mean", value.get("n"))
                if not isinstance(value, (int, float)):
                    continue
                series.setdefault((target, r["id"], name), []).append(
                    ((run.get("started") or "")[:10], value))

    if not series:
        return "no numeric metrics recorded yet"

    lines = []
    for (target, cid, name), points in sorted(series.items()):
        vals = [v for _d, v in points]
        latest = vals[-1]
        first = vals[0]
        arrow = ""
        if len(vals) > 1 and isinstance(first, (int, float)) and first:
            change = 100.0 * (latest - first) / abs(first)
            if abs(change) >= 5:
                arrow = f"  ({change:+.0f}% since {points[0][0]})"
        recent = ", ".join(f"{v:g}" for v in vals[-6:])
        lines.append(f"{target:<14} {cid:<16} {name:<26} {recent}{arrow}")
    if markdown:
        return "```\n" + "\n".join(lines) + "\n```"
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Coverage and metric trends.")
    ap.add_argument("--results-dir")
    ap.add_argument("--metrics", action="store_true", help="metric trends only")
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--matrix", action="store_true",
                     help="pass/fail pivot, all targets, for publishing")
    ap.add_argument("--target", "-t", help="limit to one target")
    args = ap.parse_args()

    catalog = load("catalog.yaml")
    targets_yaml = load("targets.yaml")
    cases = {c["id"]: c for c in catalog["cases"] if c["status"] != "idea"}
    targets = list(targets_yaml["targets"])
    if args.target:
        targets = [args.target]

    root = args.results_dir or results.results_root()
    runs = load_runs(root)
    if not runs:
        print(f"no runs found under {root}")
        print("run ./runner.py --profile smoke to create one")
        return 0

    if args.matrix:
        # Committed code only -- a status matrix published to the repository
        # is a claim about what shipped, not about what happened to be on
        # the bench a moment ago.
        clean_idx = build_index(runs, cases, clean_only=True)
        stamp = time.strftime("%Y-%m-%d", time.gmtime())
        print(f"## Test status -- {stamp}\n")
        print("Generated by `ledger.py --matrix`; do not hand-edit. Reflects committed code "
              "only -- runs against a dirty tree are excluded, not just labelled.\n")
        print(matrix(cases, targets, clean_idx))
        print()
        print(MATRIX_LEGEND)
        return 0

    idx = build_index(runs, cases)

    if not args.metrics:
        if args.markdown:
            print(f"## Coverage — {args.target}\n" if args.target
                  else "## Coverage\n")
        print(coverage(cases, targets, idx, args.markdown,
                       single_target=bool(args.target)))
        print()

    if args.markdown:
        print("## Metric trends\n")
    print(metric_trends(runs, args.markdown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
