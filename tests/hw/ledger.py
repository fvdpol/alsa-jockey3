#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""What has been tested, on what, how recently -- and which way the numbers move.

    ./ledger.py                  coverage table
    ./ledger.py --metrics        metric trends per target
    ./ledger.py --markdown       both, as markdown for publishing

The question this answers is "am I looking at stale data?". A pass from three
weeks ago still means something; it just means less. Showing the age and the
distance in commits keeps old results useful without letting them masquerade
as current -- which is the failure mode of every dashboard that only shows a
green tick.

Commit distance is measured across the whole repository on purpose. Attributing
relevance per source file would be false precision in a driver where nearly
every change touches jockey3.c.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import results          # noqa: E402

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: apt install python3-yaml")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))


def load(name):
    with open(os.path.join(HERE, name), "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


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


def commits_since(git_hash):
    """How much has changed since the revision that was tested."""
    if not git_hash:
        return None
    try:
        p = subprocess.run(
            ["git", "-C", REPO, "rev-list", "--count", f"{git_hash}..HEAD"],
            capture_output=True, text=True, timeout=15)
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


def build_index(runs):
    """Latest pass and latest attempt per (target, case)."""
    idx = {}
    for run in runs:
        target = run.get("target", "?")
        git = ((run.get("env") or {}).get("driver") or {}).get("build") or {}
        rev = git.get("git_describe") or git.get("git_hash")
        for r in run.get("results", []):
            key = (target, r["id"])
            slot = idx.setdefault(key, {"pass": None, "last": None})
            when = run.get("started")
            entry = {"when": when, "rev": rev,
                     "hash": git.get("git_hash"), "status": r["status"],
                     "run": run["_path"]}
            if slot["last"] is None or (when or "") > (slot["last"]["when"] or ""):
                slot["last"] = entry
            if r["status"] == results.PASS:
                if slot["pass"] is None or (when or "") > (slot["pass"]["when"] or ""):
                    slot["pass"] = entry
    return idx


def coverage(cases, targets, idx, markdown=False):
    rows = []
    for tname in targets:
        for cid, case in cases.items():
            slot = idx.get((tname, cid))
            if slot and slot["pass"]:
                p = slot["pass"]
                n = commits_since(p.get("hash"))
                rows.append((tname, cid, case["level"], case["mode"],
                             (p["when"] or "")[:10], p.get("rev") or "?",
                             "?" if n is None else str(n),
                             f"{age_days(p['when'])}d"))
            elif slot and slot["last"]:
                lst = slot["last"]
                rows.append((tname, cid, case["level"], case["mode"],
                             f"{lst['status']} {(lst['when'] or '')[:10]}",
                             lst.get("rev") or "?", "-", "-"))
            else:
                # Never run here. This is the row the table exists for.
                status = ("planned" if case["status"] != "implemented"
                          else "never run")
                rows.append((tname, cid, case["level"], case["mode"],
                             status, "-", "-", "-"))

    head = ("target", "case", "lvl", "mode", "last pass", "driver",
            "commits", "age")
    if markdown:
        out = ["| " + " | ".join(head) + " |",
               "|" + "|".join(["---"] * len(head)) + "|"]
        out += ["| " + " | ".join(r) + " |" for r in rows]
        return "\n".join(out)

    widths = [max(len(str(r[i])) for r in ([head] + rows))
              for i in range(len(head))]
    lines = ["  ".join(h.ljust(w) for h, w in zip(head, widths))]
    lines.append("  ".join("-" * w for w in widths))
    lines += ["  ".join(str(c).ljust(w) for c, w in zip(r, widths))
              for r in rows]
    return "\n".join(lines)


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
    ap.add_argument("--target", "-t", help="limit to one target")
    args = ap.parse_args()

    catalog = load("catalog.yaml")
    targets_yaml = load("targets.yaml")
    cases = {c["id"]: c for c in catalog["cases"]}
    targets = list(targets_yaml["targets"])
    if args.target:
        targets = [args.target]

    root = args.results_dir or results.results_root()
    runs = load_runs(root)
    if not runs:
        print(f"no runs found under {root}")
        print("run ./runner.py --profile smoke to create one")
        return 0

    idx = build_index(runs)

    if not args.metrics:
        if args.markdown:
            print("## Coverage\n")
        print(coverage(cases, targets, idx, args.markdown))
        print()

    if args.markdown:
        print("## Metric trends\n")
    print(metric_trends(runs, args.markdown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
