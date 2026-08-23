#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Manual cases: render a checklist, then read the answers back.

Manual and automated cases live in the same catalog and the same profiles, so
that coverage is one picture rather than two. What differs is only how the
verdict is obtained.

Nothing needs a path:

    ./runner.py --profile smoke              # leaves manual cases pending
    ./checklist.py --profile smoke > /tmp/checklist.md
    $EDITOR /tmp/checklist.md                # do the tests, record verdicts
    ./checklist.py --import /tmp/checklist.md

The target is detected from the running kernel, exactly as runner.py detects
it, and the run is the most recent one for that target and profile. The
rendered checklist carries the run it belongs to, so importing needs no
arguments beyond the file -- and cannot silently land in a different run than
the one it was written for, which is what passing paths by hand invites.

`--import -` reads from standard input, so a checklist can arrive over a pipe.

The generated file is plain markdown with a small machine-readable block per
case. Editing it in any text editor is the intended workflow -- a manual test
that requires a tool to record is a manual test that does not get recorded.

On import the completed checklist is copied into the run directory beside
dmesg.txt. The verdicts land in run.json, but the operator's comments and the
steps they actually followed are evidence too, and evidence belongs with the
run rather than in /tmp.
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import env, results, yamlio     # noqa: E402

if not yamlio.available():
    sys.exit("PyYAML is required: apt install python3-yaml")

HERE = os.path.dirname(os.path.abspath(__file__))

ANSWER_RE = re.compile(
    r"^\s*-\s*id:\s*(?P<id>JT-[A-Z]+-\d+)\s*\|\s*"
    r"result:\s*(?P<result>pass|fail|skip|blocked|\?)\s*\|\s*"
    r"comment:\s*(?P<comment>.*?)\s*$",
    re.M | re.I)

# Which run a checklist belongs to, carried in the file itself. An HTML comment
# because it must survive a round trip through a markdown editor without
# rendering, and because deleting it is then an obvious act rather than an
# accident. Stored relative to the results root, so a checklist filled in on
# one machine still resolves on another -- the roots differ, the run does not.
RUN_RE = re.compile(r"<!--\s*jockey3-run:\s*(?P<path>\S+)\s*-->")

STALE_SECONDS = 24 * 3600


def load(name):
    with open(os.path.join(HERE, name), "r", encoding="utf-8") as f:
        return yamlio.safe_load(f)


def pending_cases(run_path, cases):
    """The cases this run is waiting on a person for.

    Read from the run record, not re-derived from the profile. The profile
    says which cases were meant to run; the record says which ones actually
    ended up needing a human, and those are no longer the same list.

    A case whose automated form could not run -- no ppps hub on this machine,
    no loopback cable plugged in today -- demotes to manual at run time, and
    only the record knows that happened. Deriving the list from the profile
    would leave exactly the cases most at risk of being skipped silently off
    the checklist, which is the opposite of what it is for.

    It also keeps genuinely BLOCKED cases out: the profile-driven version
    could not tell "needs a person" from "cannot be done here at all", and
    rendered both.
    """
    run = results.read(os.path.join(run_path, "run.json"))
    out, seen = [], set()
    for r in run.get("results", []):
        if r.get("status") != results.PENDING or r["id"] in seen:
            continue
        case = cases.get(r["id"])
        if not case:
            continue
        seen.add(r["id"])
        params = r.get("params") or dict(case.get("params") or {})
        out.append((case, params, r.get("reason") or ""))
    return out


def render(profile, target, items, run_rel=None):
    lines = [
        f"# Manual checklist -- {profile} on {target}",
        "",
    ]
    if run_rel:
        lines += [f"<!-- jockey3-run: {run_rel} -->", ""]
    lines += [
        "Fill in `result:` for each case (pass / fail / skip / blocked) and add",
        "a comment where it helps. Then import this file into the run record:",
        "",
        "```",
        "./checklist.py --import <this file>",
        "```",
        "",
        "The run this belongs to is recorded above, so no path is needed.",
        "",
        "A comment on a passing case is not wasted effort -- \"slight crackle on",
        "headphone right, only at 96k\" is how the next bug gets found early.",
        "",
    ]
    for case, params, reason in items:
        lines.append(f"## {case['id']} -- {case['title']}")
        lines.append("")
        lines.append(f"*{case['level']} · {case['area']} · {case['mode']}*")
        lines.append("")
        # Why it is here by hand. A case that normally runs automatically is
        # worth flagging as such: the operator should know the automation
        # exists and did not run, rather than assume this is how it is done.
        #
        # Keyed on the catalog mode, not on the wording of the reason -- a
        # case that is manual by nature is not "falling back" to anything, and
        # both reasons happen to mention the checklist.
        if case.get("mode") != "manual" and reason:
            lines.append(f"**Automated form could not run here:** {reason}")
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


def humanize(seconds):
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def resolve_run(root, target, profile):
    """The run a checklist belongs to: the newest for this target and profile.

    An absent run is an error rather than an empty checklist. Answering cases
    against a run that does not exist produces verdicts with nothing to attach
    them to, and the operator finds out after doing the work rather than before.
    """
    path, age = results.find_latest_run(root, target, profile)
    if not path:
        sys.exit(
            f"no '{profile}' run recorded for target '{target}' under {root}\n"
            f"run it first:  ./runner.py --profile {profile}")
    if age is not None and age > STALE_SECONDS:
        print(f"checklist: WARNING the newest '{profile}' run for {target} is "
              f"{humanize(age)} old ({os.path.basename(path)}).\n"
              f"           Manual verdicts would be attached to that run. "
              f"Consider ./runner.py --profile {profile} first.",
              file=sys.stderr)
    return path, age


def read_text(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def run_from_checklist(text, root):
    """Find the run a completed checklist names, if it still names one."""
    m = RUN_RE.search(text)
    if not m:
        return None
    cand = os.path.join(root, m.group("path"))
    return cand if os.path.exists(os.path.join(cand, "run.json")) else None


def import_answers(text, run_path, source_name):
    answers = {}
    for m in ANSWER_RE.finditer(text):
        verdict = m.group("result").lower()
        if verdict == "?":
            continue
        answers[m.group("id").upper()] = (verdict, m.group("comment").strip())

    if not answers:
        raise SystemExit("no completed answers found -- every result is still '?'")

    run_json = os.path.join(run_path, "run.json")
    run = results.read(run_json)
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
        elif run["results"] and all(r["status"] in (results.SKIP, results.BLOCKED)
                                    for r in run["results"]):
            run["outcome"] = results.RUN_SKIP
        else:
            run["outcome"] = results.RUN_PASS

    tmp = run_json + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(run, f, indent=2)
        f.write("\n")
    os.replace(tmp, run_json)

    # The completed checklist is evidence in its own right: the verdicts are in
    # run.json, but the comments and the steps actually followed are not, and a
    # month later "slight crackle on headphone right" is the useful part.
    kept = os.path.join(run_path, "checklist.md")
    with open(kept, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")

    print(f"applied {applied} answer(s), added {added} new; "
          f"outcome now {run['outcome'].upper()}")
    print(f"run:       {run_path}")
    print(f"checklist: {os.path.relpath(kept, os.getcwd())}"
          + ("" if source_name == "-" else f"  (from {source_name})"))
    still = [r["id"] for r in run["results"] if r["status"] == results.PENDING]
    if still:
        print("still pending: " + ", ".join(sorted(set(still))))


def resolve_target(args, targets):
    """The target is detected, never defaulted to a literal.

    It selects the per-target overrides in profiles.yaml, so guessing it
    silently renders the wrong cases with the wrong parameters -- on
    armhf-prod the audio cases run at two sample rates rather than four, and a
    checklist naming four would have the operator testing something the
    profile deliberately excludes.
    """
    if args.target:
        return args.target
    name, _spec, err = env.detect_target(targets)
    if err:
        sys.exit(f"{err}\n\nOr name one with --target.")
    print(f"checklist: target '{name}' detected from the running kernel",
          file=sys.stderr)
    return name


def main():
    ap = argparse.ArgumentParser(description="Manual test checklists.")
    ap.add_argument("--profile", "-p", default="functional",
                    help="profile to render (default: functional)")
    ap.add_argument("--target", "-t",
                    help="target (default: detected from the running kernel)")
    ap.add_argument("--import", dest="import_path", nargs="?", const="-",
                    help="read a completed checklist back in; '-' or no value "
                         "reads standard input")
    ap.add_argument("--run",
                    help="run directory to use, overriding the newest one and "
                         "whatever the checklist names")
    ap.add_argument("--results-dir")
    args = ap.parse_args()

    root = args.results_dir or results.results_root()
    targets = load("targets.yaml")["targets"]

    if args.import_path:
        text = read_text(args.import_path)
        run_path = args.run or run_from_checklist(text, root)
        if not run_path:
            # The checklist named no run, or named one that is gone. Fall back
            # to the same resolution the render used, rather than refusing:
            # the answers have already been written by hand and should not be
            # lost to a deleted comment line.
            target = resolve_target(args, targets)
            run_path, _age = resolve_run(root, target, args.profile)
            print(f"checklist: no run recorded in the file; using the newest "
                  f"'{args.profile}' run for {target}", file=sys.stderr)
        run_path = run_path.rstrip("/")
        if os.path.basename(run_path) == "run.json":
            run_path = os.path.dirname(run_path)
        import_answers(text, run_path, args.import_path)
        return 0

    catalog = load("catalog.yaml")
    cases = {c["id"]: c for c in catalog["cases"]}

    target = resolve_target(args, targets)
    if target not in targets:
        sys.exit(f"unknown target '{target}'; have: {', '.join(targets)}")

    if args.run:
        run_path = args.run.rstrip("/")
        if os.path.basename(run_path) == "run.json":
            run_path = os.path.dirname(run_path)
    else:
        run_path, _age = resolve_run(root, target, args.profile)

    items = pending_cases(run_path, cases)
    if not items:
        sys.exit(f"nothing pending in {os.path.relpath(run_path, root)} -- "
                 f"every case in that run already has a verdict")
    run_rel = os.path.relpath(run_path, root)
    print(f"checklist: run {run_rel}", file=sys.stderr)
    sys.stdout.write(render(args.profile, target, items, run_rel))
    return 0


if __name__ == "__main__":
    sys.exit(main())
