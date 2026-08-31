#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run a test profile on this machine.

The runner executes ON the machine with the hardware attached -- not over ssh
from a build host. That is what lets the same suite run on a Raspberry Pi you
plugged in five minutes ago, with no network topology to debug when the
interesting failure is a kernel oops.

    ./runner.py --list                     what exists
    ./runner.py --profile smoke --dry-run  what would run here
    ./runner.py --profile smoke            run it
    ./runner.py --case JT-RATE-001         run one case

Run it as an ordinary user. Playing audio, capturing, MIDI and reading sysfs
need no privilege; the few operations that do -- loading the module, reading
dmesg, marking the log, dynamic debug, rtcwake -- go through the root-owned
helper in priv/, which is the whole privileged surface of this suite. See
priv/README.md. Running the whole thing under sudo still works, but it makes
every case root for the benefit of a handful of operations, and leaves result
files owned by root.

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
import selectors
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import (alsa, capabilities, env, kmsg, machineconf, priv,  # noqa: E402
                 results, term, yamlio)

if not yamlio.available():
    sys.exit("PyYAML is required: apt install python3-yaml")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))


def load_yaml(name):
    with open(os.path.join(HERE, name), "r", encoding="utf-8") as f:
        return yamlio.safe_load(f)


def load_all():
    catalog = load_yaml("catalog.yaml")
    targets = load_yaml("targets.yaml")
    profiles = load_yaml("profiles.yaml")
    rules = load_yaml(os.path.join("lib", "rules.yaml"))
    cases = {c["id"]: c for c in catalog["cases"] if c["status"] != "idea"}
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


def parse_param_overrides(items):
    """Turn --param KEY=VALUE strings into a parameter dict.

    Values go through json.loads first and fall back to the raw string, so
    that `capture=false` is the boolean False and `rates=[44100,96000]` is a
    list, while `mode=race` stays a string. The fallback is what makes the
    flag pleasant to type; the JSON attempt is what makes it correct. A bare
    "false" left as a string is truthy, and a run asked to turn capture off
    would quietly have run it on -- which, for the parameter sweeps this flag
    exists to serve, is worse than no flag at all.

    The last occurrence of a key wins, so a wrapper script can append its own
    overrides after the operator's.
    """
    out = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--param needs KEY=VALUE, got '{item}'")
        key, _, raw = item.partition("=")
        key = key.strip()
        if not key:
            raise SystemExit(f"--param needs a key, got '{item}'")
        try:
            out[key] = json.loads(raw)
        except ValueError:
            out[key] = raw
    return out


# Levels that test the source and the build rather than a running driver.
BUILD_LEVELS = {"L1", "L2"}

# Modes the runner executes itself. A semi-automated case IS run -- the
# machine does the setup, the actions and the bookkeeping, and stops only for
# the one thing it cannot do. Routing it to the checklist alongside the purely
# manual cases, as this used to, threw all of that away.
RUNNABLE_MODES = {"automated", "semi-automated"}

# See tests/README.md, "Test machine prerequisites". Below this, a
# marker-heavy case (JT-RATE-003: 40000 JT-MARK lines over one run) wraps the
# printk ring buffer before the run ends, and every per-change diagnostic the
# case reports goes missing along with the markers -- discovered on
# 2026-08-20 when it presented as 94% of markers "rejected" on both
# alsa-test and pi4test, neither of which had raised log_buf_len from the
# CONFIG_LOG_BUF_SHIFT=17 (128 KiB) default.
LOG_BUF_LEN_MIN = 4 * 1024 * 1024


def capability_gap(case, available):
    """Which of a case's requirements this machine cannot satisfy.

    Checked against what lib/capabilities.py resolved for the machine, not
    against a list in targets.yaml. A target is a build; a loopback cable is
    not a property of a build, and declaring it per target meant declaring it
    twice for the same EliteDesk and keeping the two in sync by hand.

    Build and component levels are exempt. Their requirements -- a kernel
    tree, cross toolchains, QEMU -- are properties of the machine doing the
    building, not of the target being built for; gating them on the target
    would refuse to cross-compile for arm64 on the x86_64 build host, which is
    the entire point of cross-compiling. Those cases check what they need
    themselves and report blocked with a specific reason.
    """
    if case.get("level") in BUILD_LEVELS:
        return []
    need = set(case.get("requires") or [])
    # A semi-automated case is one that drives the hardware but still needs a
    # person to act or to judge, so it needs `human` by construction. Derived
    # rather than listed per case: a case that forgot to list it would run
    # unattended and block forever waiting for an answer nobody is there to
    # give, which is the one failure mode this must not have.
    if case.get("mode") == "semi-automated":
        need.add("human")
    return sorted(need - set(available))


def gap_reason(gap, cap_detail):
    """Render a capability gap for a human, with the "why" a probe gave.

    Plain "needs rtc-wake" sends someone hunting for a missing Debian package
    that was never the problem -- on the Pi fleet rtcwake is installed and the
    gap is that the hardware has no RTC and no suspend-to-RAM support at all.
    Any probe can attach a reason (see _probe_rtc_wake() in
    lib/capabilities.py); this surfaces it inline instead of leaving the
    operator to go find lib/capabilities.py themselves.
    """
    bits = []
    for name in gap:
        why = (cap_detail.get(name) or {}).get("why")
        bits.append(f"{name} ({why})" if why else name)
    return ", ".join(bits)


# Capabilities that exist only to let a machine perform an action a person can
# perform by hand: switching port power, cutting the mains. Their absence
# costs automation, not coverage.
#
# Nothing else belongs here. A missing `device` cannot be worked around by an
# operator -- there is no controller to test -- and neither can a missing
# loopback cable or a missing sox. Treating those as demotable produced a
# checklist politely asking somebody to test hardware that was not plugged in.
SUBSTITUTABLE = {"usb-power", "device-power"}

# `human` demotes too, but for a different reason, and conflating the two
# would be a mistake worth naming. An actuator is missing EQUIPMENT that a
# person can replace. `human` is the person themselves being absent -- an
# unattended run -- and the answer is not to substitute anything but to leave
# the case for somebody, pending, on the checklist. Blocking instead would
# report a nightly run as having a coverage gap where it merely deferred one.
DEFERRABLE = SUBSTITUTABLE | {"human"}


def demotes_to_manual(case, gap):
    """Can this case fall back to being done by hand?

    Two conditions, and both are load-bearing. Every missing capability must
    be one a person can stand in for or be waited on for, and somebody must
    have written down how -- a case with no steps has no manual form, so it
    blocks rather than quietly becoming an instruction to do something
    unspecified.
    """
    if not case.get("steps"):
        return False
    return set(gap) <= DEFERRABLE


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


def select_cases(plan, cases, wanted):
    """Narrow a plan to --case's selection.

    Returns (plan, unknown_ids). A requested id absent from the plan is
    appended from the catalog directly, at one iteration and the catalog's
    own default params, so --case can run something the profile never
    scheduled -- an id that is not in the catalog at all comes back in
    unknown_ids instead, for the caller to report.
    """
    filtered = [p for p in plan if p[0]["id"] in wanted]
    missing = wanted - {p[0]["id"] for p in filtered}
    unknown = []
    for cid in sorted(missing):
        if cid in cases:
            filtered.append((cases[cid], 1, dict(cases[cid].get("params") or {})))
        else:
            unknown.append(cid)
    return filtered, unknown


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


def oneline(text, limit=52):
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def stream_case(cmd, cenv, timeout, on_progress=None):
    """Run a case, echoing its progress while capturing everything.

    subprocess.run(capture_output=True) holds every byte until the process
    exits, which for a case that power-cycles a device ten times is eighty
    seconds of nothing. The two channels are kept apart on purpose: stdout is
    the case's artifacts and is only captured, stderr is where it says what it
    is doing and is echoed as it arrives.

    Returns (rc, stdout, stderr), the same shape as before.
    """
    try:
        # Unbuffered, and read with os.read() rather than readline().
        #
        # This is not a style choice. With a buffered reader, one readline()
        # pulls a whole chunk off the pipe, returns its first line and keeps
        # the rest in Python's buffer -- where select() cannot see it and
        # reports the fd as not readable. Three progress lines arriving
        # together therefore surfaced one line and stalled, and a case that
        # then asked a question hung: the prompt sat in the buffer, the
        # operator saw nothing, and the case waited for an answer to a
        # question that had never been displayed. It came back only when the
        # operator pressed Enter and the resulting output woke the selector.
        p = subprocess.Popen(cmd, env=cenv, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, bufsize=0)
    except OSError as e:
        return 125, "", str(e)

    whole = {"out": bytearray(), "err": bytearray()}
    partial = bytearray()          # the unfinished trailing line of stderr

    def emit(final=False):
        """Hand every complete line of stderr to the printer.

        A line ends at a newline, or at a CARRIAGE RETURN -- which a case uses
        to mark a line as transient, something it is about to rewrite in
        place, like a countdown. Splitting on both is what lets that work
        through a pipe: the printer decides whether the terminal can honor it.
        """
        if not on_progress:
            return
        while True:
            nl = partial.find(b"\n")
            cr = partial.find(b"\r")
            if nl < 0 and cr < 0:
                break
            if nl >= 0 and (cr < 0 or nl < cr):
                idx, transient = nl, False
            else:
                idx, transient = cr, True
            text = bytes(partial[:idx]).decode("utf-8", "replace")
            del partial[:idx + 1]
            # A CRLF is one ending, not a transient line followed by nothing.
            if transient and partial[:1] == b"\n":
                del partial[:1]
                transient = False
            on_progress(text, transient)
        # At end of stream a trailing fragment is all there will ever be, so
        # show it rather than swallow it.
        if final and partial:
            on_progress(bytes(partial).decode("utf-8", "replace"), False)
            partial.clear()

    def finish(rc, note=None):
        return (rc, bytes(whole["out"]).decode("utf-8", "replace"),
                note if note is not None
                else bytes(whole["err"]).decode("utf-8", "replace"))

    sel = selectors.DefaultSelector()
    sel.register(p.stdout, selectors.EVENT_READ, "out")
    sel.register(p.stderr, selectors.EVENT_READ, "err")
    deadline = time.time() + timeout
    live = 2
    try:
        while live:
            left = deadline - time.time()
            if left <= 0:
                p.kill()
                p.wait()
                return finish(124, f"timed out after {timeout}s")
            # Woken at least once a second even in silence, so a hung case is
            # noticed at the deadline rather than whenever it next speaks.
            for key, _mask in sel.select(timeout=min(left, 1.0)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    live -= 1
                    if key.data == "err":
                        emit(final=True)
                    continue
                whole[key.data] += chunk
                if key.data == "err":
                    partial += chunk
                    emit()
        p.wait(timeout=max(1.0, deadline - time.time()))
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        return finish(124, f"timed out after {timeout}s")
    except KeyboardInterrupt:
        # Ctrl-C used to kill the runner and leave the case running, still
        # holding the MIDI port or the audio device -- so the NEXT run failed
        # for a reason that had nothing to do with the driver, and looked like
        # a hang. Take the child with us.
        p.kill()
        p.wait()
        raise
    finally:
        sel.close()
    return finish(p.returncode)


def failure_reason(err, out, rc):
    """Why a case failed, in one line.

    The LAST meaningful line, not the first. Case.done() prints its failure
    summary last, and everything before it may be progress chatter -- taking
    the head would report "cycle 1/10: power off" as the reason a case failed
    on cycle nine.
    """
    for text in (err, out):
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        if lines:
            return lines[-1][:200]
    return f"exit {rc}"


# How a verdict looks at a glance. The word is still there for anything
# parsing the output; the mark is for the person watching it happen.
MARKS = {results.PASS: "✓", results.FAIL: "✗",
         results.SKIP: "–", results.BLOCKED: "⊘",
         results.PENDING: "?"}

MARK_STYLES = {results.PASS: ("bold", "green"), results.FAIL: ("bold", "red"),
               results.SKIP: ("dim",), results.BLOCKED: ("yellow",),
               results.PENDING: ("cyan",)}


def mark(status, style=None):
    glyph = MARKS.get(status, " ")
    if style is None:
        return glyph
    return style(glyph, *MARK_STYLES.get(status, ()))


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
        "JT_ATTENDED": "0" if ctx.get("unattended") else "1",
    })

    cmd = [path] + list(case.get("args") or [])
    # --quiet hides progress, but a semi-automated case asks questions on
    # this channel. Suppressing it would leave the operator staring at a
    # silent terminal while the case waits for an answer to a question they
    # were never shown -- a deadlock, not a cosmetic loss.
    show = not ctx.get("quiet") or case.get("mode") == "semi-automated"
    # An explicit --timeout always wins. Otherwise a case that documents its
    # own expected duration in catalog.yaml (an endurance run like
    # JT-RATE-003) is trusted over the CLI's generic default, which exists
    # for the common case of a case with no idea how long it takes.
    timeout = ctx["timeout"]
    if timeout is None:
        timeout = case.get("timeout", 3600)
    rc, out, err = stream_case(cmd, cenv, timeout,
                               ctx["printer"] if show else None)

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
        r.reason = failure_reason(err, out, rc)
    elif rc == 3:
        r.status = results.BLOCKED
        r.reason = failure_reason(err, out, rc)
    else:
        r.status = results.FAIL
        r.reason = failure_reason(err, out, rc)

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
        if not args.dry_run:
            ok, why = priv.available()
            if not ok:
                problems.append(f"privileged helper unusable: {why}")
            elif priv.stale():
                problems.append(
                    f"{priv.HELPER} differs from tests/hw/priv/jockey3-testctl "
                    f"-- re-run `sudo tests/hw/priv/install.sh`")
        if card is None:
            problems.append("no Jockey 3 card found in /proc/asound")
        buf = env.log_buf_len()
        if buf is not None and buf < LOG_BUF_LEN_MIN:
            problems.append(
                f"printk ring buffer is {buf // 1024}K, below the {LOG_BUF_LEN_MIN // (1024 * 1024)}M "
                f"a marker-heavy case needs -- kernel-log markers will be "
                f"silently lost mid-run and per-change diagnostics will be "
                f"suppressed; add log_buf_len=4M to this machine's kernel "
                f"command line and reboot (see tests/README.md, 'Test "
                f"machine prerequisites')")
        servers = alsa.active_sound_servers()
        if servers:
            problems.append(
                "sound server(s) running: " + ", ".join(servers) +
                " -- may hold the card open; stop them yourself if a case "
                "fails to get exclusive access "
                "(tests/hw/actions/sound_server.sh disable)")
    needed = set()
    for case, iterations, _ in plan:
        if iterations > 0 and case.get("mode") in RUNNABLE_MODES:
            if "sox" in (case.get("requires") or []):
                needed.update(["sox", "aplay"])
            if case.get("area") in ("PCM", "AUDIO", "RATE"):
                needed.update(["aplay", "arecord"])
            if case.get("area") == "MIDI":
                needed.add("amidi")
            # rtcwake is deliberately not checked here. It lives in /sbin,
            # which is not on a non-root PATH, and it is invoked inside the
            # privileged helper rather than by the case -- so `which rtcwake`
            # would report it missing on a machine where it works fine.
    missing = alsa.missing_tools(sorted(needed))
    if missing:
        problems.append("missing tools: " + ", ".join(missing))
    return card, cid, problems


def main():
    ap = argparse.ArgumentParser(description="Run a Jockey 3 test profile.")
    # No default here: it comes from this machine's config, so that the bare
    # command does the right thing on the build server and on the bench
    # without anyone having to remember which. --list still works with none.
    ap.add_argument("--profile", "-p", default=None)
    ap.add_argument("--force", action="store_true",
                    help="run a profile this machine does not list as "
                         "applicable")
    ap.add_argument("--target", "-t", help="override target auto-detection")
    ap.add_argument("--case", "-c", action="append",
                    help="run only these case ids (repeatable)")
    ap.add_argument("--param", "-P", action="append", metavar="KEY=VALUE",
                    help="override a case parameter for this run (repeatable, "
                         "applies to every planned case). The value is parsed "
                         "as JSON when it can be, so capture=false is the "
                         "boolean and rates=[44100,96000] is the list; "
                         "anything else is taken as a string. The resolved "
                         "parameters are what run.json records.")
    ap.add_argument("--list", "-l", action="store_true")
    ap.add_argument("--dry-run", "-n", action="store_true")
    ap.add_argument("--operator", default=os.environ.get("USER", ""))
    ap.add_argument("--note", default="",
                    help="free-text note recorded in run.json, for keeping "
                         "track of what this run/experiment was for (e.g. "
                         "which bench build or bpftrace script was attached) "
                         "without relying on chat history or memory")
    ap.add_argument("--quiet", "-q", action="store_true",
                    help="do not echo each case's progress as it runs; keep "
                         "only the per-case verdict")
    ap.add_argument("--unattended", action="store_true",
                    help="nobody is at the keyboard: withhold the 'human' "
                         "capability, so cases needing a person are recorded "
                         "as pending rather than waiting for an answer that "
                         "is not coming. For CI.")
    ap.add_argument("--results-dir")
    ap.add_argument("--kernel-src",
                    help="kernel tree for build-only runs (default $KERNEL_SRC "
                         "or ~/sound)")
    ap.add_argument("--timeout", type=int, default=None,
                    help="per-case timeout in seconds, overriding a case's "
                         "own 'timeout' in catalog.yaml if it has one "
                         "(default: 3600, or the case's own)")
    args = ap.parse_args()

    _catalog, cases, targets, profiles, rules = load_all()

    if args.list:
        cmd_list(cases, targets, profiles)
        return 0

    if not args.profile:
        args.profile = machineconf.get("profiles.default", env="JOCKEY3_PROFILE",
                                       default="smoke")

    # Advisory only, and deliberately so. Which cases CAN run here is already
    # settled by capabilities, case by case and with a reason attached; a
    # second list saying which profiles belong on this machine would be a
    # second source of truth about the same thing, and would silently exclude
    # cases the moment the two drifted. So this catches "wrong window" -- a
    # regression pass started on the build server -- and gets out of the way.
    applicable = machineconf.section("profiles").get("applicable")
    if applicable and args.profile not in applicable and not args.force:
        print(f"warning: {args.profile} is not listed as applicable on this "
              f"machine ({', '.join(applicable)}).", file=sys.stderr)
        print("Cases that cannot run here will be recorded as skipped with a "
              "reason. Pass --force to silence this.", file=sys.stderr)

    # The plan needs the target (for per-target overrides) and the target needs
    # the plan (to know whether this run touches a running driver at all).
    # Break the cycle with a provisional plan: overrides can change how many
    # times a case runs, never whether the profile is build-only in nature.
    #
    # --case DOES change which cases are in it, though, and has to be applied
    # here too, not only to the final plan below: needs_running_kernel() saw
    # the whole default profile even for `--case JT-CODEC-001`, which drags
    # in every L3+ case the profile happens to schedule and makes an L2-only
    # run demand a running kernel it never touches. On a build host that
    # forces --target by hand -- and even then, resolve_target() had already
    # picked env.kernel_info() (the running kernel) over kernel_tree_info(),
    # so the "tree" key that env.built_driver_info() needs to identify a
    # build-only run's driver revision was never in `kernel` to begin with.
    # A run like that keeps its driver identity unknown forever, which reads
    # as permanently stale in ledger.py regardless of how often it is rerun.
    provisional = resolve_plan(args.profile, None, cases, profiles)
    if args.case:
        provisional, _unknown = select_cases(provisional, cases, set(args.case))
    target_name, target_spec, kernel, target_problems = resolve_target(
        args, targets["targets"], provisional)
    if not target_name:
        for p in target_problems:
            print(f"error: {p}", file=sys.stderr)
        return 3

    plan = resolve_plan(args.profile, target_name, cases, profiles)
    if args.case:
        plan, unknown = select_cases(plan, cases, set(args.case))
        for cid in unknown:
            print(f"error: unknown case {cid}", file=sys.stderr)
        if unknown:
            return 3

    # Applied last, over the catalog, the profile entry and the per-target
    # override alike -- the operator on the bench is the most specific source
    # there is. Applied to every planned case, which is why it is normally
    # paired with --case: a parameter sweep is a run of one case at a time.
    overrides = parse_param_overrides(args.param)
    if overrides:
        plan = [(case, iterations, {**params, **overrides})
                for case, iterations, params in plan]

    caps, cap_detail = capabilities.resolve(attended=not args.unattended)

    card, cid, problems = preflight(target_name, target_spec, plan, args)
    problems = target_problems + problems
    problems += env.verify_target(target_spec, kernel)
    if cap_detail.get("_note"):
        problems.append(cap_detail["_note"])

    print(f"target   {target_name}   "
          f"({target_spec.get('arch')}, {target_spec.get('flavor')} config)")
    print(f"kernel   {kernel.get('release') or '?'}   "
          f"[identified from the {kernel.get('source') or '?'}]")
    print(f"machine  {os.uname().nodename}   (context only, not the target)")
    print(f"profile  {args.profile}")
    if overrides:
        print("param    " + ", ".join(f"{k}={json.dumps(v)}"
                                      for k, v in sorted(overrides.items()))
              + "   [--param, applied to every case in the plan]")
    print(f"card     {'hw:%d (%s)' % (card, cid) if card is not None else 'not found'}")
    print(f"have     {', '.join(sorted(caps)) or 'nothing detected'}"
          + ("   [--unattended: no human]" if args.unattended else ""))
    if problems:
        print("\npreflight:")
        for p in problems:
            print(f"  ! {p}")

    print("\nplan:")
    runnable = 0
    manual = 0
    # Everything that will land a CaseResult in run.json -- runnable, manual
    # fallback, disabled-on-target (SKIP) and blocked (BLOCKED) alike. Only a
    # case whose status is not yet "implemented" writes nothing at all, so
    # that is the one thing that should make "nothing to run" true below --
    # not just the runnable/manual subset, which used to make a run of only
    # disabled or blocked cases print "nothing to run" and skip writing them.
    recorded = 0
    for case, iterations, params in plan:
        gap = capability_gap(case, caps)
        if iterations <= 0:
            note = "disabled on this target"
            recorded += 1
        elif gap and demotes_to_manual(case, gap):
            note = "manual fallback: no " + gap_reason(gap, cap_detail)
            manual += 1
            recorded += 1
        elif gap:
            note = "blocked: needs " + gap_reason(gap, cap_detail)
            recorded += 1
        elif case["status"] != "implemented":
            note = "planned -- not implemented yet"
        elif case["mode"] not in RUNNABLE_MODES:
            note = "manual -- use checklist.py"
            manual += 1
            recorded += 1
        else:
            note = f"{iterations}x"
            runnable += 1
            recorded += 1
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

    if not recorded:
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

    # Enables the firmware dev_dbg on a module that is already loaded. A load
    # performed by the suite gets the rule from priv.load_module() instead,
    # which is the only way to have it in place before probe runs.
    fw_ok, fw_err = env.enable_firmware_debug()

    # Bounds this run's dmesg.txt. The kernel ring buffer holds many hours --
    # around eighteen on the test machines -- so an untrimmed capture is mostly
    # earlier runs, and reading it raw makes a clean run look like a failing
    # one. Two 2026-08-17 runs of JT-RATE-001 that stalled zero times out of
    # 244 shipped dmesg.txt files containing 62 and 10 "Capture URB has
    # stalled." lines respectively, every one of them from a previous run.
    # Written before the cases start so a driver reload performed by the suite
    # still lands inside the window.
    run_marker = kmsg.Marker(f"run#{stamp}")
    run_marker.write()

    run = results.Run(
        run_id=f"{target_name}-{stamp}-{args.profile}",
        profile=args.profile, target=target_name,
        started=results.utc_iso(started), operator=args.operator,
        note=args.note,
    )
    run.env = env.capture(targets["targets"], args.kernel_src)
    run.env["target_identified_from"] = kernel.get("source")
    run.env["target_explicit"] = bool(args.target)
    run.env["preflight"] = problems
    # Which preconditions held, and how each was decided. Without this a pass
    # from a day the loopback cable was connected reads identically to one
    # taken with it coiled on the bench, and ledger.py cannot tell the
    # difference between coverage and its absence.
    run.env["capabilities"] = cap_detail
    run.env["attended"] = not args.unattended
    if not fw_ok:
        run.env["firmware_debug_error"] = fw_err

    style = term.Style()
    live = term.Live()

    ctx = {
        "run_path": run_path,
        "card": card,
        "device": alsa.device_name(card) if card is not None else None,
        "classifier": kmsg.Classifier(rules),
        "timeout": args.timeout,
        "investigate": [],
        "unclassified": [],
        # Echoed live. Indented under the case header so a case's own account
        # of what it is doing is visibly subordinate to the runner's verdict.
        # Styling happens HERE and not in the case: what reaches stderr.txt
        # stays plain text.
        "printer": lambda line, transient=False: live.write(
            term.decorate(style, line) if not transient
            else style(line, "dim"), transient),
        "live": live,
        "quiet": args.quiet,
        "unattended": args.unattended,
    }

    run_json = os.path.join(run_path, "run.json")
    aborted = False

    print(f"\nresults -> {run_path}\n")
    for case, iterations, params in plan:
        if iterations <= 0:
            # Disabled by a profile's per-target override -- not applicable
            # here by design, e.g. JT-PM-001 on a Pi with no RTC. Recorded as
            # SKIP rather than silently dropped: without an entry in run.json,
            # ledger.py's matrix cannot tell "not applicable to this target"
            # apart from "nobody has run this here yet", and shows both the
            # same blank cell.
            r = results.CaseResult(
                id=case["id"], mode=case.get("mode", ""),
                level=case.get("level", ""), area=case.get("area", ""),
                status=results.SKIP, reason="disabled on this target",
                started=results.utc_iso())
            run.add(r)
            print(f"  {mark(results.SKIP, style)} {case['id']:<16} "
                  f"SKIP     disabled on this target")
            continue
        if case["status"] != "implemented":
            continue
        gap = capability_gap(case, caps)
        if gap:
            # A missing capability is not the end of the case. If somebody
            # wrote down how to do it by hand, it demotes to manual and lands
            # in the checklist: an automated form that cannot run today is a
            # reason to ask a person, not a reason to lose the coverage.
            # Without steps there is nothing to fall back to, so it blocks.
            if demotes_to_manual(case, gap):
                r = results.CaseResult(
                    id=case["id"], mode="manual", level=case.get("level", ""),
                    area=case.get("area", ""), status=results.PENDING,
                    reason=("no " + gap_reason(gap, cap_detail)
                            + " -- do it by hand via checklist.py"),
                    # The RESOLVED parameters, not the catalog's. checklist.py
                    # renders what the record says, so omitting them would put
                    # the catalog defaults in front of the operator and quietly
                    # undo every per-target override -- asking for four sample
                    # rates on the Pi 1B, where the profile says two.
                    params=params, started=results.utc_iso())
                run.add(r)
                print(f"  {mark(results.PENDING, style)} {case['id']:<16} "
                      f"PENDING  manual fallback (no {gap_reason(gap, cap_detail)})")
                continue
            r = results.CaseResult(
                id=case["id"], mode=case.get("mode", ""),
                level=case.get("level", ""), area=case.get("area", ""),
                status=results.BLOCKED, reason="needs " + gap_reason(gap, cap_detail),
                started=results.utc_iso())
            run.add(r)
            print(f"  {mark(results.BLOCKED, style)} {case['id']:<16} "
                  f"BLOCKED  needs {gap_reason(gap, cap_detail)}")
            continue
        if case["mode"] not in RUNNABLE_MODES:
            r = results.CaseResult(
                id=case["id"], mode=case["mode"], level=case.get("level", ""),
                area=case.get("area", ""), status=results.PENDING,
                reason="manual -- answer via checklist.py",
                params=params, started=results.utc_iso())
            run.add(r)
            print(f"  {mark(results.PENDING, style)} {case['id']:<16} "
                  f"PENDING  manual")
            continue

        for i in range(1, iterations + 1):
            tag = f"{case['id']}#{i}" if iterations > 1 else case["id"]
            # Announced before it runs, not after. A case that power-cycles a
            # device ten times takes over a minute, and a runner that says
            # nothing until it finishes is indistinguishable from one that has
            # hung.
            counter = f" ({i}/{iterations})" if iterations > 1 else ""
            # One character of prefix on both this line and the verdict, so
            # the case ids line up in a column and the marks read down it.
            print(style(f"\n  ▶ {tag:<16}", "bold")
                  + f" {oneline(case.get('title'))}{counter}", flush=True)
            r = run_case(case, i, params, ctx)
            run.add(r)
            extra = f"  {r.reason}" if r.reason else ""
            live.close()
            print(f"  {mark(r.status, style)} {tag:<16} "
                  f"{r.status.upper():<8} {r.duration_s:>6.1f}s{extra}")
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
    elif run.results and all(r.status in (results.SKIP, results.BLOCKED)
                             for r in run.results):
        # Nothing actually ran -- every selected case was disabled on this
        # target or blocked on a missing capability. That is not a pass:
        # a `--case JT-PM-001` run on a Pi with no RTC tested nothing, and
        # calling it PASS would read as "the driver was verified" when it
        # was not exercised at all.
        run.outcome = results.RUN_SKIP
    else:
        run.outcome = results.RUN_PASS

    run.unclassified = ctx["unclassified"][:200]
    run.ended = results.utc_iso()
    if not run.env["driver"]["loaded"]:
        # A build-only run never loads anything, so driver_info() (captured
        # before the gates ran) can only ever say "unknown". By now the
        # "build" gate, if it ran, has produced a .ko and a manifest for it --
        # read the identity back from the file instead of leaving it unknown.
        built = env.built_driver_info(kernel.get("tree") or "")
        if built and built.get("build"):
            run.env["driver"] = built
    # Read once and reuse: the firmware probe wants the whole buffer, since a
    # module loaded before the run announced itself before the marker, while
    # dmesg.txt wants only this run's slice.
    full_log = kmsg.read_log()
    run.env["firmware"] = env.firmware_from_log(full_log)
    if run.env["firmware"] is None and run.env["driver"]["loaded"]:
        run.env["firmware_note"] = (
            "firmware revision not seen in the kernel log -- dynamic debug was "
            "not enabled before the module was loaded, so this run does not "
            "know which firmware it tested")

    # Trimmed to this run's own window; see run_marker above. Falls back to the
    # whole log if the marker never made it, and says so in the file's header.
    dmesg_text, dmesg_trimmed = kmsg.run_log(full_log, run_marker)
    run.env["dmesg_trimmed_to_run"] = dmesg_trimmed

    results.write(run, run_json)

    with open(os.path.join(run_path, "dmesg.txt"), "w", encoding="utf-8") as f:
        f.write(dmesg_text)

    # Fold this run's URB-restart timings into the growing dataset. Best-effort:
    # a prod-kernel run with dynamic debug on contributes samples, anything else
    # is a no-op. Never fail a completed run over the bookkeeping.
    try:
        from lib import restart_timing
        data = restart_timing.load()
        record, _reason = restart_timing.source_from_run(run.as_dict(), dmesg_text)
        if record and restart_timing.add_source(data, record):
            restart_timing.save(data)
            n = sum(sum(b.values()) for b in record["hist"].values())
            print(f"restart_timing: +{n} samples ({', '.join(sorted(record['hist']))})")
    except Exception as exc:  # noqa: BLE001 -- diagnostics only
        print(f"restart_timing: skipped ({exc})")

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
