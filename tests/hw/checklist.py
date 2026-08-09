#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Manual cases: render a checklist, then read the answers back.

Manual and automated cases live in the same catalog and the same profiles, so
that coverage is one picture rather than two. What differs is only how the
verdict is obtained.

    ./checklist.py --profile functional > checklist.md   # go and do them
    ./checklist.py --import checklist.md --run <run.json>

The generated file is plain markdown with a small machine-readable block per
case. Editing it in any text editor is the intended workflow -- a manual test
that requires a tool to record is a manual test that does not get recorded.
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import results          # noqa: E402

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: apt install python3-yaml")

HERE = os.path.dirname(os.path.abspath(__file__))

ANSWER_RE = re.compile(
    r"^\s*-\s*id:\s*(?P<id>JT-[A-Z]+-\d+)\s*\|\s*"
    r"result:\s*(?P<result>pass|fail|skip|blocked|\?)\s*\|\s*"
    r"comment:\s*(?P<comment>.*?)\s*$",
    re.M | re.I)


def load(name):
    with open(os.path.join(HERE, name), "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def manual_cases(profile, target, cases, profiles):
    prof = profiles["profiles"].get(profile)
    if not prof:
        raise SystemExit(f"unknown profile '{profile}'")
    overrides = (prof.get("overrides") or {}).get(target, {})
    out = []
    for entry in prof["cases"]:
        case = cases.get(entry["id"])
        if not case or case.get("mode") == "automated":
            continue
        iterations = overrides.get(case["id"], {}).get(
            "iterations", entry.get("iterations", 1))
        if iterations <= 0:
            continue
        params = dict(case.get("params") or {})
        params.update(entry.get("params") or {})
        params.update((overrides.get(case["id"]) or {}).get("params") or {})
        out.append((case, params))
    return out


def render(profile, target, items):
    lines = [
        f"# Manual checklist -- {profile} on {target}",
        "",
        "Fill in `result:` for each case (pass / fail / skip / blocked) and add",
        "a comment where it helps. Then import this file into the run record:",
        "",
        "```",
        f"./checklist.py --import <this file> --run <path to run.json>",
        "```",
        "",
        "A comment on a passing case is not wasted effort -- \"slight crackle on",
        "headphone right, only at 96k\" is how the next bug gets found early.",
        "",
    ]
    for case, params in items:
        lines.append(f"## {case['id']} -- {case['title']}")
        lines.append("")
        lines.append(f"*{case['level']} · {case['area']} · {case['mode']}*")
        lines.append("")
        if params:
            lines.append("Parameters: "
                         + ", ".join(f"`{k}={v}`" for k, v in params.items()))
            lines.append("")
        for step in case.get("steps") or []:
            lines.append(f"- [ ] {' '.join(str(step).split())}")
        if case.get("steps"):
            lines.append("")
        if case.get("pass"):
            lines.append("**Pass when:** " + " ".join(case["pass"].split()))
            lines.append("")
        if case.get("notes"):
            lines.append("> " + " ".join(case["notes"].split()))
            lines.append("")
        lines.append(f"- id: {case['id']} | result: ? | comment:")
        lines.append("")
    return "\n".join(lines) + "\n"


def import_answers(path, run_path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    answers = {}
    for m in ANSWER_RE.finditer(text):
        verdict = m.group("result").lower()
        if verdict == "?":
            continue
        answers[m.group("id").upper()] = (verdict, m.group("comment").strip())

    if not answers:
        raise SystemExit("no completed answers found -- every result is still '?'")

    run = results.read(run_path)
    by_id = {}
    for r in run["results"]:
        by_id.setdefault(r["id"], []).append(r)

    applied, added = 0, 0
    for cid, (verdict, comment) in answers.items():
        rows = by_id.get(cid)
        if rows:
            for r in rows:
                r["status"] = verdict
                r["note"] = comment
                r["reason"] = ""
            applied += 1
        else:
            # A manual case answered but absent from the run: record it rather
            # than discard it. Coverage that happened is coverage, even if the
            # runner was not involved.
            run["results"].append({
                "id": cid, "iteration": 1, "status": verdict,
                "mode": "manual", "level": "", "area": "",
                "started": results.utc_iso(), "duration_s": 0.0,
                "params": {}, "metrics": {}, "dmesg": {},
                "note": comment, "reason": "imported from checklist",
            })
            added += 1

    counts = {}
    for r in run["results"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    run["counts"] = counts

    # Re-derive the outcome now that manual verdicts are in. INVESTIGATE is
    # never overwritten: a kernel defect outranks any number of manual passes.
    if run.get("outcome") != results.RUN_INVESTIGATE:
        if any(r["status"] == results.FAIL for r in run["results"]):
            run["outcome"] = results.RUN_FAIL
        elif any(r["status"] == results.PENDING for r in run["results"]):
            run["outcome"] = results.PENDING
        else:
            run["outcome"] = results.RUN_PASS

    tmp = run_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(run, f, indent=2)
        f.write("\n")
    os.replace(tmp, run_path)

    print(f"applied {applied} answer(s), added {added} new; "
          f"outcome now {run['outcome'].upper()}")
    still = [r["id"] for r in run["results"] if r["status"] == results.PENDING]
    if still:
        print("still pending: " + ", ".join(sorted(set(still))))


def main():
    ap = argparse.ArgumentParser(description="Manual test checklists.")
    ap.add_argument("--profile", "-p", default="functional")
    ap.add_argument("--target", "-t", default="x86_64-prod")
    ap.add_argument("--import", dest="import_path",
                    help="read a completed checklist back in")
    ap.add_argument("--run", help="run.json to import into")
    args = ap.parse_args()

    if args.import_path:
        if not args.run:
            sys.exit("--import needs --run <path to run.json>")
        import_answers(args.import_path, args.run)
        return 0

    catalog = load("catalog.yaml")
    profiles = load("profiles.yaml")
    cases = {c["id"]: c for c in catalog["cases"]}
    items = manual_cases(args.profile, args.target, cases, profiles)
    if not items:
        sys.exit(f"no manual cases in profile '{args.profile}' "
                 f"for target '{args.target}'")
    sys.stdout.write(render(args.profile, args.target, items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
