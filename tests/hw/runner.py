#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run a test profile on this machine.

The runner executes ON the machine with the hardware attached -- not over ssh
from a build host. That is what lets the same suite run on a Raspberry Pi you
plugged in five minutes ago, with no network topology to debug when the
interesting failure is a kernel oops.

    ./runner.py --list                     what exists
    ./runner.py --profile smoke --dry-run  what would run here
    sudo ./runner.py --profile smoke       run it
    sudo ./runner.py --case JT-RATE-001    run one case

Case contract
-------------
A case is any executable under cases/. The runner passes:

    JT_CASE_ID     the case id
    JT_ITERATION   1-based iteration number
    JT_PARAMS      parameters as JSON
    JT_CARD        ALSA card index, or empty if no card was found
    JT_DEVICE      hw:<index>,0, or empty
    JT_WORKDIR     a directory to write artifacts into
    JT_RESULT_FILE where to write a JSON result object
    JT_REPO        the repository root

and interprets the exit code as: 0 pass, 2 skip, 3 blocked, anything else
fail. The result file is optional; it supplies metrics and a note. Kernel-log
classification is the runner's job, not the case's -- a case cannot be trusted
to notice an oops it caused.
"""

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import alsa, env, kmsg, results          # noqa: E402

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: apt install python3-yaml")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))


def load_yaml(name):
    with open(os.path.join(HERE, name), "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_all():
    catalog = load_yaml("catalog.yaml")
    targets = load_yaml("targets.yaml")
    profiles = load_yaml("profiles.yaml")
    rules = load_yaml(os.path.join("lib", "rules.yaml"))
    cases = {c["id"]: c for c in catalog["cases"]}
    return catalog, cases, targets, profiles, rules


def resolve_plan(profile_name, target_name, cases, profiles):
    """Expand a profile into an ordered list of (case, iterations, params).

    Per-target overrides carry iterations *and* parameters, because scaling a
    test down is not always a matter of running it fewer times: on the Pi 1B
    the high sample rates are skipped outright, not merely run less often.
    """
    prof = profiles["profiles"].get(profile_name)
    if not prof:
        raise SystemExit(f"unknown profile '{profile_name}'; have: "
                         + ", ".join(profiles["profiles"]))
    overrides = (prof.get("overrides") or {}).get(target_name, {})
    plan = []
    for entry in prof["cases"]:
        cid = entry["id"]
        case = cases.get(cid)
        if not case:
            raise SystemExit(f"profile references unknown case {cid}")
        iterations = entry.get("iterations", 1)
        params = dict(case.get("params") or {})
        params.update(entry.get("params") or {})
        ov = overrides.get(cid) or {}
        iterations = ov.get("iterations", iterations)
        params.update(ov.get("params") or {})
        plan.append((case, iterations, params))
    return plan


# Levels that test the source and the build rather than a running driver.
BUILD_LEVELS = {"L1", "L2"}


def capability_gap(case, target_spec):
    """Which of a case's requirements this target cannot satisfy.

    Build and component levels are exempt. Their requirements -- a kernel
    tree, cross toolchains, QEMU -- are properties of the machine doing the
    building, not of the target being built for; gating them on the target
    would refuse to cross-compile for arm64 on the x86_64 build host, which is
    the entire point of cross-compiling. Those cases check what they need
    themselves and report blocked with a specific reason.
    """
    if case.get("level") in BUILD_LEVELS:
        return []
    have = set((target_spec or {}).get("capabilities") or [])
    need = set(case.get("requires") or [])
    return sorted(need - have)


def needs_running_kernel(plan):
    """Does anything in this plan actually exercise the loaded driver?

    A build-only run identifies itself from the kernel tree it is building,
    not from the machine it happens to be sitting on -- running the L1 gates
    on the build host is testing the configuration in ~/sound, which may be
    for an entirely different architecture than the build host's own.
    """
    for case, iterations, _params in plan:
        if iterations > 0 and case.get("level") not in BUILD_LEVELS:
            return True
    return False


def resolve_target(args, targets, plan):
    """Return (name, spec, kernel_facts, problems)."""
    problems = []
    if needs_running_kernel(plan):
        kernel = env.kernel_info()
        source = "running kernel"
    else:
        kernel = env.kernel_tree_info(args.kernel_src)
        source = "kernel tree"
        if kernel is None:
            src = args.kernel_src or os.environ.get("KERNEL_SRC") or "~/sound"
            problems.append(f"no configured kernel tree at {src} "
                            f"(set KERNEL_SRC or pass --kernel-src)")
            kernel = env.kernel_info()
            source = "running kernel (no tree found)"

    if args.target:
        spec = targets.get(args.target)
        if spec is None:
            return None, None, kernel, [f"unknown target '{args.target}'"]
        return args.target, spec, kernel, problems

    name, spec, err = env.detect_target(targets, kernel)
    if err:
        problems.append(err)
    return name, spec, kernel, problems


def case_path(case):
    exe = case.get("exec")
    if not exe:
        return None
    return os.path.join(HERE, exe)


def run_case(case, iteration, params, ctx):
    """Execute one automated case and classify what the kernel said about it."""
    started = time.time()
    r = results.CaseResult(
        id=case["id"], iteration=iteration, mode=case.get("mode", "automated"),
        level=case.get("level", ""), area=case.get("area", ""),
        started=results.utc_iso(started), params=params,
    )

    path = case_path(case)
    if not path or not os.path.exists(path):
        r.status = results.BLOCKED
        r.reason = (f"no executable for {case['id']} "
                    f"({case.get('exec') or 'exec not set'})")
        r.duration_s = 0.0
        return r

    workdir = os.path.join(ctx["run_path"], "cases",
                           f"{case['id']}-{iteration}")
    os.makedirs(workdir, exist_ok=True)
    result_file = os.path.join(workdir, "result.json")

    marker = kmsg.Marker(f"{case['id']}#{iteration}")
    marker.write()

    cenv = dict(os.environ)
    cenv.update({
        "JT_CASE_ID": case["id"],
        "JT_ITERATION": str(iteration),
        "JT_PARAMS": json.dumps(params),
        "JT_CARD": str(ctx["card"]) if ctx["card"] is not None else "",
        "JT_DEVICE": ctx["device"] or "",
        "JT_WORKDIR": workdir,
        "JT_RESULT_FILE": result_file,
        "JT_REPO": REPO,
    })

    cmd = [path] + list(case.get("args") or [])
    try:
        proc = subprocess.run(cmd, env=cenv, capture_output=True, text=True,
                              timeout=ctx["timeout"])
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", f"timed out after {ctx['timeout']}s"
    except OSError as e:
        rc, out, err = 125, "", str(e)

    with open(os.path.join(workdir, "stdout.txt"), "w", encoding="utf-8") as f:
        f.write(out or "")
    with open(os.path.join(workdir, "stderr.txt"), "w", encoding="utf-8") as f:
        f.write(err or "")

    lines = kmsg.slice_since(kmsg.read_log(), marker)
    buckets, log_metrics = ctx["classifier"].classify(
        lines, case.get("expect_dmesg"))
    r.dmesg = kmsg.summarize(buckets)

    for name, value in log_metrics.items():
        if isinstance(value, list):
            r.metrics[name] = kmsg.histogram(value)
        else:
            r.metrics[name] = value

    if os.path.exists(result_file):
        try:
            with open(result_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            r.metrics.update(payload.get("metrics") or {})
            r.note = payload.get("note", "")
        except (OSError, ValueError) as e:
            r.note = f"result file unreadable: {e}"

    if rc == 0:
        r.status = results.PASS
    elif rc == 2:
        r.status = results.SKIP
        r.reason = (err or out or "").strip()[:200]
    elif rc == 3:
        r.status = results.BLOCKED
        r.reason = (err or out or "").strip()[:200]
    else:
        r.status = results.FAIL
        r.reason = (err or out or f"exit {rc}").strip()[:200]

    # A driver message that is ours and unexpected fails the case even when
    # the case itself was happy. The case does not know what it provoked.
    if r.status == results.PASS and buckets[kmsg.UNEXPECTED]:
        r.status = results.FAIL
        r.reason = f"unexpected driver message: {buckets[kmsg.UNEXPECTED][0][:160]}"

    if buckets[kmsg.INVESTIGATE]:
        ctx["investigate"].extend(buckets[kmsg.INVESTIGATE])
    if buckets[kmsg.UNCLASSIFIED]:
        ctx["unclassified"].extend(buckets[kmsg.UNCLASSIFIED])

    r.duration_s = round(time.time() - started, 3)
    return r


def cmd_list(cases, targets, profiles):
    print("Profiles:")
    for name, p in profiles["profiles"].items():
        desc = " ".join((p.get("description") or "").split())
        print(f"  {name:<12} {len(p['cases']):>2} cases  {desc[:60]}")
    print("\nTargets:")
    for name, t in targets["targets"].items():
        note = " (not yet available)" if t.get("status") else ""
        print(f"  {name:<14} {t.get('machine', '')}{note}")
    print("\nCases:")
    impl = sum(1 for c in cases.values() if c["status"] == "implemented")
    for c in cases.values():
        mark = "x" if c["status"] == "implemented" else " "
        print(f"  [{mark}] {c['id']:<16} {c['level']} {c['mode']:<15} {c['title']}")
    print(f"\n{impl}/{len(cases)} implemented")


def preflight(target_name, target_spec, plan, args):
    """Report what stands between here and a real run, all at once.

    Deliberately not fail-fast: finding out about three missing packages one
    run at a time is how an evening disappears.
    """
    problems = []
    card, cid = alsa.find_card()
    # Only complain about hardware when something in the plan wants it. A
    # build-only run on the build host is not missing anything by not having a
    # DJ controller plugged into it.
    if needs_running_kernel(plan):
        if os.geteuid() != 0 and not args.dry_run:
            problems.append("not root -- module load/unload and /dev/kmsg "
                            "markers will not work (re-run with sudo)")
        if card is None:
            problems.append("no Jockey 3 card found in /proc/asound")
    needed = set()
    for case, iterations, _ in plan:
        if iterations > 0 and case.get("mode") == "automated":
            if "sox" in (case.get("requires") or []):
                needed.update(["sox", "aplay"])
            if case.get("area") in ("PCM", "AUDIO", "RATE"):
                needed.update(["aplay", "arecord"])
            if case.get("area") == "MIDI":
                needed.add("amidi")
            if "rtc-wake" in (case.get("requires") or []):
                needed.add("rtcwake")
    missing = alsa.missing_tools(sorted(needed))
    if missing:
        problems.append("missing tools: " + ", ".join(missing))
    return card, cid, problems


def main():
    ap = argparse.ArgumentParser(description="Run a Jockey 3 test profile.")
    ap.add_argument("--profile", "-p", default="smoke")
    ap.add_argument("--target", "-t", help="override target auto-detection")
    ap.add_argument("--case", "-c", action="append",
                    help="run only these case ids (repeatable)")
    ap.add_argument("--list", "-l", action="store_true")
    ap.add_argument("--dry-run", "-n", action="store_true")
    ap.add_argument("--operator", default=os.environ.get("USER", ""))
    ap.add_argument("--results-dir")
    ap.add_argument("--kernel-src",
                    help="kernel tree for build-only runs (default $KERNEL_SRC "
                         "or ~/sound)")
    ap.add_argument("--timeout", type=int, default=3600,
                    help="per-case timeout in seconds")
    args = ap.parse_args()

    _catalog, cases, targets, profiles, rules = load_all()

    if args.list:
        cmd_list(cases, targets, profiles)
        return 0

    # The plan needs the target (for per-target overrides) and the target needs
    # the plan (to know whether this run touches a running driver at all).
    # Break the cycle with a provisional plan: overrides can change how many
    # times a case runs, never whether the profile is build-only in nature.
    provisional = resolve_plan(args.profile, None, cases, profiles)
    target_name, target_spec, kernel, target_problems = resolve_target(
        args, targets["targets"], provisional)
    if not target_name:
        for p in target_problems:
            print(f"error: {p}", file=sys.stderr)
        return 3

    plan = resolve_plan(args.profile, target_name, cases, profiles)
    if args.case:
        wanted = set(args.case)
        plan = [p for p in plan if p[0]["id"] in wanted]
        missing = wanted - {p[0]["id"] for p in plan}
        if missing:
            # Allow running a case that is not in the profile at all.
            for cid in sorted(missing):
                if cid in cases:
                    plan.append((cases[cid], 1, dict(cases[cid].get("params") or {})))
                else:
                    print(f"error: unknown case {cid}", file=sys.stderr)
                    return 3

    card, cid, problems = preflight(target_name, target_spec, plan, args)
    problems = target_problems + problems
    problems += env.verify_target(target_spec, kernel)

    print(f"target   {target_name}   "
          f"({target_spec.get('arch')}, {target_spec.get('flavor')} config)")
    print(f"kernel   {kernel.get('release') or '?'}   "
          f"[identified from the {kernel.get('source') or '?'}]")
    print(f"machine  {os.uname().nodename}   (context only, not the target)")
    print(f"profile  {args.profile}")
    print(f"card     {'hw:%d (%s)' % (card, cid) if card is not None else 'not found'}")
    if problems:
        print("\npreflight:")
        for p in problems:
            print(f"  ! {p}")

    print("\nplan:")
    runnable = 0
    manual = 0
    for case, iterations, params in plan:
        gap = capability_gap(case, target_spec)
        if iterations <= 0:
            note = "disabled on this target"
        elif gap:
            note = "blocked: needs " + ", ".join(gap)
        elif case["status"] != "implemented":
            note = "planned -- not implemented yet"
        elif case["mode"] != "automated":
            note = "manual -- use checklist.py"
            manual += 1
        else:
            note = f"{iterations}x"
            runnable += 1
        extra = ""
        if params:
            bits = [f"{k}={v}" for k, v in params.items()]
            extra = "  " + " ".join(bits)
        print(f"  {case['id']:<16} {note:<34}{extra}")
    summary = f"\n{runnable} case(s) would execute"
    if manual:
        summary += f", {manual} manual case(s) recorded as pending"
    print(summary)

    if args.dry_run:
        return 0

    if not runnable and not manual:
        print("nothing to run", file=sys.stderr)
        return 1
    # A profile of only manual cases still produces a run record: the pending
    # entries are what checklist.py imports answers into. Refusing to create
    # one would leave manual results with nowhere to land.

    # ------------------------------------------------------------- execute
    started = time.time()
    stamp = results.utc_stamp(started)
    root = args.results_dir or results.results_root()
    run_path = results.run_dir(root, target_name, args.profile, stamp)
    os.makedirs(os.path.join(run_path, "cases"), exist_ok=True)

    # Must happen before the module is loaded: the firmware revision is
    # emitted once, at probe, and only as a dev_dbg.
    fw_ok, fw_err = env.enable_firmware_debug()

    stopped = alsa.stop_sound_server()

    run = results.Run(
        run_id=f"{target_name}-{stamp}-{args.profile}",
        profile=args.profile, target=target_name,
        started=results.utc_iso(started), operator=args.operator,
    )
    run.env = env.capture(targets["targets"], args.kernel_src)
    run.env["target_identified_from"] = kernel.get("source")
    run.env["target_explicit"] = bool(args.target)
    run.env["preflight"] = problems
    run.env["sound_server_stopped"] = stopped
    if not fw_ok:
        run.env["firmware_debug_error"] = fw_err

    ctx = {
        "run_path": run_path,
        "card": card,
        "device": alsa.device_name(card) if card is not None else None,
        "classifier": kmsg.Classifier(rules),
        "timeout": args.timeout,
        "investigate": [],
        "unclassified": [],
    }

    run_json = os.path.join(run_path, "run.json")
    aborted = False

    print(f"\nresults -> {run_path}\n")
    for case, iterations, params in plan:
        if iterations <= 0 or case["status"] != "implemented":
            continue
        gap = capability_gap(case, target_spec)
        if gap:
            r = results.CaseResult(
                id=case["id"], mode=case.get("mode", ""),
                level=case.get("level", ""), area=case.get("area", ""),
                status=results.BLOCKED, reason="needs " + ", ".join(gap),
                started=results.utc_iso())
            run.add(r)
            print(f"  {case['id']:<16} BLOCKED  needs {', '.join(gap)}")
            continue
        if case["mode"] != "automated":
            r = results.CaseResult(
                id=case["id"], mode=case["mode"], level=case.get("level", ""),
                area=case.get("area", ""), status=results.PENDING,
                reason="manual -- answer via checklist.py",
                started=results.utc_iso())
            run.add(r)
            print(f"  {case['id']:<16} PENDING  manual")
            continue

        for i in range(1, iterations + 1):
            r = run_case(case, i, params, ctx)
            run.add(r)
            tag = f"{case['id']}#{i}" if iterations > 1 else case["id"]
            extra = f"  {r.reason}" if r.reason else ""
            print(f"  {tag:<16} {r.status.upper():<8} {r.duration_s:>6.1f}s{extra}")
            results.write(run, run_json)

            if ctx["investigate"]:
                aborted = True
                break
        if aborted:
            break

    # A defect is not a test failure. The machine needs attention, the right
    # response is to open an issue, and continuing would only produce results
    # from a kernel that is already in an undefined state.
    if ctx["investigate"]:
        run.outcome = results.RUN_INVESTIGATE
    elif any(r.status == results.FAIL for r in run.results):
        run.outcome = results.RUN_FAIL
    elif any(r.status == results.PENDING for r in run.results):
        # Manual cases are still unanswered, so the run is not yet a pass.
        # checklist.py --import settles it.
        run.outcome = results.PENDING
    else:
        run.outcome = results.RUN_PASS

    run.unclassified = ctx["unclassified"][:200]
    run.ended = results.utc_iso()
    run.env["firmware"] = env.firmware_from_log(kmsg.read_log())
    if run.env["firmware"] is None and run.env["driver"]["loaded"]:
        run.env["firmware_note"] = (
            "firmware revision not seen in the kernel log -- dynamic debug was "
            "not enabled before the module was loaded, so this run does not "
            "know which firmware it tested")
    results.write(run, run_json)

    with open(os.path.join(run_path, "dmesg.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(kmsg.read_log()) + "\n")

    alsa.start_sound_server(stopped)

    counts = run.counts()
    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"outcome: {run.outcome.upper()}")
    if ctx["investigate"]:
        print("\nA kernel defect was detected; the run was abandoned.")
        print("This is not a test failure -- open an issue and investigate:")
        for line in ctx["investigate"][:5]:
            print(f"  {line}")
    if ctx["unclassified"]:
        print(f"\n{len(ctx['unclassified'])} unclassified kernel message(s) "
              f"need a look; see run.json")
    print(f"\n{run_json}")

    return 0 if run.outcome == results.RUN_PASS else 1


if __name__ == "__main__":
    sys.exit(main())
