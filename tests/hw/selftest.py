#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for the test framework itself.

    ./selftest.py

Needs no hardware and no root. Run it after touching lib/, rules.yaml,
catalog.yaml, targets.yaml or profiles.yaml.

The framework decides what counts as a pass, so a mistake in here does not
produce a visible failure -- it produces a green run that means nothing. That
is the one failure mode worth spending test code on: everything below either
proves a message lands in the bucket that changes the verdict, or proves a
target is identified as what it actually is.
"""

import io
import json
import ast
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import alsa, env, kmsg, restart_timing, results, yamlio    # noqa: E402

if not yamlio.available():
    sys.exit("PyYAML is required: apt install python3-yaml")

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER = "snd-reloop-jockey3 1-3:1.0: "

_failures = []
_checks = 0


def check(ok, label, detail=""):
    global _checks
    _checks += 1
    if not ok:
        _failures.append(f"{label}{(': ' + detail) if detail else ''}")
    print(f"  {'ok ' if ok else 'FAIL'} {label}"
          + (f"   {detail}" if detail and not ok else ""))


def load(name):
    with open(os.path.join(HERE, name), "r", encoding="utf-8") as f:
        return yamlio.safe_load(f)


# --------------------------------------------------------------- classifier

def test_classifier(rules):
    print("\nkernel message classification")
    c = kmsg.Classifier(rules)

    # Timestamped, as dmesg actually renders them -- see test_kunit_on_target.
    #
    # NOTE: the two "waited N ms for reset completion" lines below are a rule
    # the driver no longer exercises. 775b70e added that dev_dbg and c8fff65
    # ("prepare for batching") removed it, so reset_wait_ms has been empty ever
    # since and this fixture only proves the rule agrees with itself. Kept so
    # the rule still works if the line is restored -- see
    # re/rate_change_stall.md, which is where the decision to restore it lives.
    T = "[ 5870.058393] "
    lines = [
        T + DRIVER + "Capture URB has stalled.",
        T + DRIVER + "jockey3_pcm_prepare waited 336 ms for reset completion.",
        T + DRIVER + "jockey3_pcm_prepare waited 1012 ms for reset completion.",
        T + DRIVER + "Inconsistent URB in-flight count: playback=2 != 0",
        T + DRIVER + "Some message nobody has ever seen",
        T + "usb 1-3: new high-speed USB device number 7 using xhci_hcd",
        T + "wlan0: authenticate with aa:bb:cc:dd:ee:ff",
        T + "BUG: unable to handle kernel NULL pointer dereference at 0000",
        T + DRIVER + "Playback URB has stalled.",
    ]
    b, m = c.classify(lines, [r"Capture URB has stalled\."])
    got = kmsg.summarize(b)

    # The stall is the whole point: it is expected on a healthy build, so it
    # must not be able to fail a run.
    check(got[kmsg.EXPECTED] == 4, "a stall is expected, not a failure",
          str(got))
    check(got[kmsg.UNEXPECTED] == 2,
          "driver self-check and unknown driver message both fail", str(got))
    check(got[kmsg.UNRELATED] == 1, "other subsystems' noise is ignored")
    check(got[kmsg.UNCLASSIFIED] == 1,
          "an unrecognized line is surfaced, not swallowed")
    check(got[kmsg.INVESTIGATE] == 1, "a kernel defect escalates")

    h = kmsg.histogram(m.get("reset_wait_ms", []))
    check(h and h["n"] == 2 and h["max"] == 1012,
          "reset delays are captured as a distribution", str(h))
    check(m.get("stalls_capture") == 1 and m.get("stalls_playback") == 1,
          "stalls are counted per direction")

    # A defect inside an otherwise expected message is still a defect.
    b2, _ = c.classify([DRIVER + "Capture URB has stalled. BUG: at foo.c:1"],
                       [r"Capture URB has stalled\."])
    check(len(b2[kmsg.INVESTIGATE]) == 1,
          "escalation wins over an expected-message match")

    # dev_dbg() traces are classified on syslog priority, not per-message
    # rules: with `dmesg --raw` every line keeps its "<N>" prefix, and any
    # line that is ours and KERN_DEBUG (<7>) is a trace. So an operator can
    # turn dynamic debug up without any case going red over a message no rule
    # names.
    dbg = "<7>[ 5870.1] " + DRIVER + "brand-new trace nobody whitelisted x=3\n"
    b3, _ = c.classify([dbg], [])
    check(len(b3[kmsg.EXPECTED]) == 1 and not b3[kmsg.UNEXPECTED]
          and not b3[kmsg.UNCLASSIFIED],
          "an unwhitelisted KERN_DEBUG line from our driver is a trace")

    # ...but priority is not a free pass: a defect in a debug line still
    # escalates, because the investigate pass runs before the level check.
    b4, _ = c.classify(["<7>[ 5870.2] " + DRIVER + "trace BUG: at foo.c:1\n"], [])
    check(len(b4[kmsg.INVESTIGATE]) == 1,
          "a defect in a KERN_DEBUG line still escalates")

    # A non-debug driver message no rule names is still unexpected -- the
    # shortcut is for KERN_DEBUG only.
    b5, _ = c.classify(["<4>[ 5870.3] " + DRIVER + "something new at warning level\n"], [])
    check(len(b5[kmsg.UNEXPECTED]) == 1,
          "a KERN_WARNING line no rule names is still a failure")

    # The rate limiter's own summary: bare function name, no device prefix, no
    # priority -- covered by an explicit rule, not by the shortcut.
    b6, m6 = c.classify(["jockey3_stream_streaming_healthy: 7 callbacks suppressed\n"], [])
    check(len(b6[kmsg.EXPECTED]) == 1 and not b6[kmsg.UNCLASSIFIED],
          "a ratelimit 'callbacks suppressed' summary is benign")
    check(m6.get("dbg_ratelimit_suppressed") == 1,
          "suppressed-callback summaries are counted")


def test_wedged_device(rules):
    """The 2026-08-11 lockup, replayed line for line.

    Every message below was in the log of a device that had stopped answering
    while still enumerated, and NONE of them could fail a run at the time.
    The drain error was the earliest by several minutes and was invisible to
    the classifier because it names the ALSA card, not the module.
    """
    print("\nwedged-device signatures")
    c = kmsg.Classifier(rules)
    T = "[20748.130875] "
    lines = [
        T + "sound midiC1D0: rawmidi drain error (avail = 4090, buffer_size = 4096)",
        T + DRIVER + "Failed to initialize device to change rate: -110",
        T + DRIVER + "Failed to submit playback URB 0: -2",
        T + DRIVER + "Failed to submit MIDI IN URB: -2",
    ]
    b, _m = c.classify(lines, [])
    check(len(b[kmsg.UNEXPECTED]) == 4,
          "every symptom of the wedged device fails the run",
          str(kmsg.summarize(b)))
    check(not b[kmsg.UNCLASSIFIED],
          "and none of them is merely flagged for someone to notice")

    # The benign teardown message must NOT have been widened by any of this:
    # a resubmit racing a deliberate stop is still normal.
    b2, _ = c.classify([T + DRIVER + "Failed to resubmit playback URB: -2"], [])
    check(len(b2[kmsg.EXPECTED]) == 1,
          "while a resubmit lost to teardown stays benign -- a different "
          "message and a different event", str(kmsg.summarize(b2)))


def test_watchdog(rules):
    """The URB liveness watchdog's messages, and why they are bucketed as they are.

    The watchdog exists because the 2026-08-11 wedge was a SILENCE: every error
    path in the driver hangs off a URB completion, so when completions stopped,
    nothing ran and nothing was logged for about fourteen minutes. These rules
    are what turn that silence into a red run.

    Stage 3 trimmed the watchdog to edge-triggered onset/recovery only -- no
    periodic heartbeat, and therefore no separate "still stalled" driver_fail
    rule either. What now turns a stall nothing is dealing with into a red run
    is jockey3_recover_urb_stream()'s own give-up lines (recovery budget
    exhausted, or still stalled after a full reset), tested in
    test_recovery_giveup() below rather than here.
    """
    print("\nURB liveness watchdog signatures")
    c = kmsg.Classifier(rules)
    T = "[21337.057880] "

    onset = (T + DRIVER + "Playback URB stream stalled: no completion for 517 ms "
             "(8 URBs in flight, substream open)")
    recovery = T + DRIVER + "Capture URB stream recovered after 2043 ms"

    b, m = c.classify([onset, recovery], [])
    check(len(b[kmsg.EXPECTED]) == 2,
          "a stall that recovered is counted, not failed", str(kmsg.summarize(b)))

    h = kmsg.histogram(m.get("urb_stall_onset_ms", []))
    check(h and h["n"] == 1 and h["max"] == 517,
          "the measured onset age is captured, not the threshold", str(h))
    h = kmsg.histogram(m.get("urb_stall_recovered_ms", []))
    check(h and h["max"] == 2043, "and so is the outage duration", str(h))

    # The onset must stay countable even when a case declares it expected --
    # benign short-circuits before expect_dmesg, which is what makes
    # JT-RATE-004 possible at all.
    b, m = c.classify([onset], [r"URB stream stalled"])
    check(len(b[kmsg.EXPECTED]) == 1 and m.get("urb_stall_onset_ms"),
          "an expected onset is still measured")

    # The common ending, and the reason it exists. The first hardware run with
    # the watchdog recorded 103 onsets and zero recoveries: every recovery path
    # goes through a URB stop/start, so the restart got there first every time
    # and used to clear the flag silently, leaving every onset unpaired.
    restart = T + DRIVER + "Capture URB stream restarted after stalling for 1219 ms"
    b, m = c.classify([restart], [])
    check(len(b[kmsg.EXPECTED]) == 1, "a restart closing an outage is counted",
          str(kmsg.summarize(b)))
    h = kmsg.histogram(m.get("urb_stall_restarted_ms", []))
    check(h and h["max"] == 1219, "with the outage it ended", str(h))


def test_recovery_giveup(rules):
    """jockey3_recover_urb_stream()'s ladder, shared since Stage 3 by all
    four call-site contexts (rate change, opening a capture stream,
    preparing a playback stream, watchdog) and both directions.

    Each step is benign and tracked on a healthy build; only the two give-up
    lines fail a run, since Stage 3 trimmed the watchdog's periodic heartbeat
    and these are what now turn a stall nothing is dealing with into a red
    run instead.

    The fixture text for each context is copied verbatim from the format
    string in jockey3_recover_urb_stream() plus the literal context argument
    each call site passes -- not composed to read naturally -- because a
    fixture that merely sounds right is exactly what let the watchdog
    context ship with no coverage at all: rules.yaml matched "(the
    watchdog)" while the driver has only ever logged "(watchdog)", and nothing
    caught the mismatch until it misclassified a real run on arm64-prod
    (2026-08-24).
    """
    print("\nrecovery-ladder signatures")
    c = kmsg.Classifier(rules)
    T = "[21337.057880] "

    light_retry = [
        T + DRIVER + "Capture stream stalled (rate change); restarting URBs "
                     "to recover",
        T + DRIVER + "Playback stream stalled (rate change); restarting URBs "
                     "to recover",
        T + DRIVER + "Capture stream stalled (opening a capture stream); "
                     "restarting URBs to recover",
        T + DRIVER + "Playback stream stalled (preparing a playback stream); "
                     "restarting URBs to recover",
        T + DRIVER + "Capture stream stalled (watchdog); restarting URBs "
                     "to recover",
        T + DRIVER + "Playback stream stalled (watchdog); restarting URBs "
                     "to recover",
    ]
    b, _ = c.classify(light_retry, [])
    check(not b[kmsg.UNEXPECTED] and not b[kmsg.UNCLASSIFIED],
          "every light-retry context is benign", str(kmsg.summarize(b)))

    escalate = [
        T + DRIVER + "Capture stream still stalled after URB restart; queuing "
                     "full USB reset (rate change)",
        T + DRIVER + "Playback stream still stalled after URB restart; queuing "
                     "full USB reset (opening a capture stream)",
        T + DRIVER + "Playback stream still stalled after URB restart; queuing "
                     "full USB reset (preparing a playback stream)",
    ]
    b, _ = c.classify(escalate, [])
    check(not b[kmsg.UNEXPECTED] and not b[kmsg.UNCLASSIFIED],
          "escalating to a full reset is benign, from any context",
          str(kmsg.summarize(b)))

    giveup = [
        T + DRIVER + "Capture stream still stalled after full USB reset; "
                     "hardware may need power-cycling (rate change)",
        T + DRIVER + "Playback stream still stalled after URB restart; "
                     "recovery budget exhausted, not resetting "
                     "(preparing a playback stream)",
    ]
    b, _ = c.classify(giveup, [])
    check(len(b[kmsg.UNEXPECTED]) == 2,
          "both give-up paths fail the run, regardless of context",
          str(kmsg.summarize(b)))

    # The escalation line and the budget-exhausted give-up line share the
    # prefix "still stalled after URB restart" -- benign and driver_fail
    # tested together, not in isolation, is what would have caught either
    # pattern accidentally swallowing the other (the exact failure mode
    # milestone 13's own history warns about: one pattern reading zeros where
    # the other should have fired).
    b, _ = c.classify([
        T + DRIVER + "Capture stream still stalled after URB restart; "
                     "queuing full USB reset (rate change)",
        T + DRIVER + "Capture stream still stalled after URB restart; "
                     "recovery budget exhausted, not resetting (rate change)",
    ], [])
    check(len(b[kmsg.EXPECTED]) == 1 and len(b[kmsg.UNEXPECTED]) == 1,
          "escalation and budget-exhausted land in different buckets even "
          "when classified together", str(kmsg.summarize(b)))


def test_error_handling(rules):
    """Signatures added with the issue #26 error-handling work.

    The -ENOENT cascade these describe is driver-triggered and deterministic:
    usb_set_interface() disables an interface's endpoints before sending
    SET_INTERFACE and does not re-enable them when that request fails.
    """
    print("\nerror-handling signatures")
    c = kmsg.Classifier(rules)
    T = "[21588.823651] "
    lines = [
        T + DRIVER + "Firmware version read failed: -110",
        T + DRIVER + "Failed to clear halt on EP 0x86: -110",
        T + DRIVER + "Started only 5/8 playback and 8/8 capture URBs; ring will not refill",
        T + DRIVER + "Endpoints are disabled after a failed rate change; "
                     "the device needs a reset to restore them",
        T + DRIVER + "Failed to start URBs during initialization: -19",
        T + DRIVER + "Playback URB cancelled without a driver-initiated stop: -2",
    ]
    b, _ = c.classify(lines, [])
    check(len(b[kmsg.UNEXPECTED]) == len(lines),
          "every new error signature fails the run", str(kmsg.summarize(b)))
    check(not b[kmsg.UNCLASSIFIED], "and none of them is merely surfaced")

    # Verbatim from the 2026-08-11 16:01 functional run, where these fired five
    # times on an ordinary rmmod. The driver is bound to two interfaces and the
    # USB core unbinds them one at a time, so the first one's endpoints are
    # flushed while the other is still bound; the URBs came back -ESHUTDOWN with
    # no teardown flag set yet. Kept as a fixture because the message is only
    # worth having if it stays silent on a normal unload.
    unload = [
        T + DRIVER + "MIDI IN URB cancelled without a driver-initiated stop: -108",
        T + DRIVER + "Playback URB cancelled without a driver-initiated stop: -108",
    ]
    b, _ = c.classify(unload, [])
    check(len(b[kmsg.UNEXPECTED]) == 2,
          "an unsolicited cancellation still fails if the driver ever emits one",
          str(kmsg.summarize(b)))

    # The dev_dbg counterpart is a trace, not a fault: nothing to start because
    # the device had already gone.
    b, _ = c.classify(
        [T + DRIVER + "Could not start URBs after a rate change: device is gone"], [])
    check(len(b[kmsg.EXPECTED]) == 1,
          "while an unplug during a restart stays benign", str(kmsg.summarize(b)))

    # The .prepare message must not have been folded into the counted one.
    b, m = c.classify([T + DRIVER + "Playback URB has stalled."], [])
    check(len(b[kmsg.EXPECTED]) == 1 and m.get("stalls_playback") == 1,
          "and the poll helper's own stall message stays a counted metric")


def test_kunit_on_target(rules):
    """Verbatim transcript of a module load, timestamps and all.

    These fixtures used to be bare message text. Every anchored rule therefore
    matched in the selftest and none of them matched a real dmesg line, which
    is how 1602 lines came back unclassified from the first hardware run
    against a rule set that had always looked correct. The prefixes below are
    the point of the test as much as the messages are.
    """
    print("\ncodec KUnit output at module load")
    c = kmsg.Classifier(rules)
    b, m = c.classify([
        "[ 5871.543119]     KTAP version 1",
        "[ 5871.543120]     # Subtest: ploytec-codec",
        "[ 5871.543121]     # module: snd_reloop_jockey3",
        "[ 5871.543122]     ok 1 ploytec_test_encode_known_vectors",
        "[ 5871.543123]     ok 5 zeros",
        "[ 5871.543124]     ok 12 random00",
        "[ 5871.543125]     1..75",
        "[ 5872.282635]     # ploytec-codec: pass:21 fail:0 skip:0 total:21",
        "[ 5872.290143]     # Totals: pass:75 fail:0 skip:0 total:75",
        "[ 5872.296836]     ok 1 ploytec-codec",
    ], [])
    check(not b[kmsg.UNEXPECTED] and not b[kmsg.UNCLASSIFIED],
          "a passing suite does not pollute the run", str(kmsg.summarize(b)))
    check(m.get("kunit_cases_passed_on_target") == [21],
          "the top-level case count is recorded as a metric", str(m))

    b2, _ = c.classify(
        ["[ 5872.1] not ok 3 ploytec_test_encode_is_linear"], [])
    check(len(b2[kmsg.UNEXPECTED]) == 1, "a failing codec case fails the run")

    # A parameterized failure carries nothing identifying it as ours, so the
    # suite summary is what has to catch it.
    b3, _ = c.classify([
        "[ 5872.2]     not ok 5 zeros",
        "[ 5872.3]     # ploytec-codec: pass:20 fail:1 skip:0 total:21",
    ], [])
    check(len(b3[kmsg.UNEXPECTED]) == 1,
          "a failing parameterized case fails the run via the suite summary",
          str(kmsg.summarize(b3)))


# ------------------------------------------------------------ target model

def _kernel(arch, release, debug=(), config_available=True):
    return {"arch": arch, "release": release, "debug_options": list(debug),
            "config_available": config_available, "source": "selftest"}


def test_targets(targets):
    print("\ntarget identification")
    T = targets["targets"]
    DBG = ["KASAN", "PROVE_LOCKING"]

    for label, kern, want in [
        ("debug kernel", _kernel("x86_64", "7.2.0-rc5-alsa-debug", DBG),
         "x86_64-debug"),
        ("prod kernel", _kernel("x86_64", "7.2.0-alsa-prod"), "x86_64-prod"),
        # setlocalversion appends "+" to an untagged git tree; an exact suffix
        # match would reject every kernel built from a working tree.
        ("trailing + from an untagged tree",
         _kernel("x86_64", "7.2.0-alsa-prod+"), "x86_64-prod"),
        ("same LOCALVERSION, different arch",
         _kernel("arm64", "6.12.0-alsa-prod+"), "arm64-prod"),
        ("32-bit arm", _kernel("armhf", "6.12.0-alsa-prod"), "armhf-prod"),
    ]:
        name, _spec, err = env.detect_target(T, kern)
        check(name == want, label, err or f"got {name}")

    # Refusing to guess is the point: a run recorded against the wrong target
    # corrupts that target's history.
    name, _s, err = env.detect_target(T, _kernel("x86_64", "7.0.12-1-pve"))
    check(name is None and err, "an unlabelled kernel is refused, not guessed")
    check("LOCALVERSION" in (err or ""), "the refusal says how to fix it")

    print("\nconfiguration cross-check")
    for label, kern, target, expect in [
        ("prod label, KASAN compiled in",
         _kernel("x86_64", "7.2-alsa-prod", DBG), "x86_64-prod",
         "expects a production kernel but KASAN"),
        ("debug label, no debug options",
         _kernel("x86_64", "7.2-alsa-debug"), "x86_64-debug",
         "expects a debug kernel but none of"),
        ("config unreadable",
         _kernel("arm64", "6.12-alsa-prod", (), False), "arm64-prod",
         "could not be cross-checked"),
    ]:
        problems = env.verify_target(T[target], kern)
        check(any(expect in p for p in problems), f"mislabel: {label}",
              str(problems))

    consistent = env.verify_target(
        T["x86_64-debug"], _kernel("x86_64", "7.2-alsa-debug", DBG))
    check(not consistent, "a correctly labelled kernel raises nothing")

    print("\narchitecture normalization")
    for machine, want in [("armv6l", "armhf"), ("armv7l", "armhf"),
                          ("aarch64", "arm64"), ("i686", "i386"),
                          ("amd64", "x86_64"), ("x86_64", "x86_64")]:
        check(env.canon_arch(machine) == want, f"uname {machine} -> {want}")

    check(env.arch_from_config({"ARM64": "y", "ARM": "y"}) == "arm64",
          "arm64 configs set CONFIG_ARM too, and must not read as armhf")


def test_build_id():
    print("\nmodule identity")
    # namesz=4 ("GNU\0"), descsz=4, type=3, then the 4-byte id.
    note = (b"\x04\x00\x00\x00\x04\x00\x00\x00\x03\x00\x00\x00"
            b"GNU\x00" + b"\xde\xad\xbe\xef")
    check(env.parse_build_id_note(note) == "deadbeef",
          "a build-id note is parsed")
    check(env.parse_build_id_note(b"") is None, "a truncated note is not fatal")


# ------------------------------------------------------------- consistency

def test_catalog(catalog, targets, profiles):
    print("\ncatalog, targets and profiles")
    cases = {c["id"]: c for c in catalog["cases"]}
    caps = set(targets["capabilities"])

    check(len(cases) == len(catalog["cases"]), "case ids are unique")

    bad = [f"{c['id']} needs {r}" for c in catalog["cases"]
           for r in (c.get("requires") or []) if r not in caps]
    check(not bad, "every requirement names a known capability", str(bad[:3]))

    bad = [f"{t} has {cp}" for t, d in targets["targets"].items()
           for cp in (d.get("capabilities") or []) if cp not in caps]
    check(not bad, "every target capability is known", str(bad[:3]))

    missing = [f"{t}.{k}" for t, d in targets["targets"].items()
               for k in ("arch", "flavor", "localversion")if not d.get(k)]
    check(not missing, "every target declares arch, flavor and localversion",
          str(missing))

    lv = {}
    for t, d in targets["targets"].items():
        lv.setdefault((d.get("arch"), d.get("localversion")), []).append(t)
    clashes = {k: v for k, v in lv.items() if len(v) > 1}
    check(not clashes, "no two targets share an arch and LOCALVERSION",
          str(clashes))

    bad = []
    for p, d in profiles["profiles"].items():
        for e in d["cases"]:
            if e["id"] not in cases:
                bad.append(f"{p} -> {e['id']}")
        for t, o in (d.get("overrides") or {}).items():
            if t not in targets["targets"]:
                bad.append(f"{p} overrides unknown target {t}")
            for cid in o:
                if cid not in cases:
                    bad.append(f"{p}/{t} -> {cid}")
    check(not bad, "profiles reference only known cases and targets",
          str(bad[:3]))

    import runner
    bad = [c["id"] for c in catalog["cases"]
           if c["status"] == "implemented"
           and c["mode"] in runner.RUNNABLE_MODES and not c.get("exec")]
    check(not bad, "implemented runnable cases name an executable", str(bad))

    # A semi-automated case only runs when somebody is at the keyboard, so it
    # needs a manual form for the nights when nobody is.
    bad = [c["id"] for c in catalog["cases"]
           if c["status"] == "implemented" and c["mode"] == "semi-automated"
           and not c.get("steps")]
    check(not bad, "semi-automated cases carry manual fallback steps", str(bad))

    bad = [c["id"] for c in catalog["cases"]
           if c.get("exec") and not os.path.exists(os.path.join(HERE, c["exec"]))]
    check(not bad, "every named executable exists", str(bad))

    bad = [c["id"] for c in catalog["cases"]
           if c["status"] == "implemented" and c["mode"] == "manual"
           and not c.get("steps")]
    check(not bad, "implemented manual cases have steps to follow", str(bad))

    # A case that needs hardware not every machine has must say how to do it
    # by hand, or the coverage simply vanishes on the machines without it.
    # HARDWARE is the subset that is genuinely rig-specific -- `device` and
    # `root` are not, since a hardware case is pointless without them.
    bad = [c["id"] for c in catalog["cases"]
           if c["status"] == "implemented"
           and runner.SUBSTITUTABLE & set(c.get("requires") or [])
           and not c.get("steps")]
    check(not bad, "cases gated on an actuator carry manual fallback steps",
          str(bad))


def test_capabilities(targets):
    """Resolution, and the rule that a declaration can only take away.

    The dangerous direction is a declaration that GRANTS something absent: a
    run would record a pass for a loopback measurement with no cable in the
    socket, and nothing downstream could tell.
    """
    print("\ncapabilities")
    from lib import capabilities as cap

    check(set(cap.ALL) == set(targets["capabilities"]),
          "targets.yaml documents exactly the known capabilities",
          str(set(cap.ALL) ^ set(targets["capabilities"])))
    check(not (set(cap.PROBED) & set(cap.DECLARED)),
          "no capability is both probed and declared")

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "caps.yaml")

        with open(path, "w") as f:
            f.write("version: 1\ncapabilities:\n  speakers: true\n"
                    "  loopback-cable: false\n")
        declared, note = cap.load_declared(path)
        check(declared == {"speakers": True, "loopback-cable": False},
              "declarations are read", str(declared))
        check(note is None, "a clean file produces no complaint")

        # The veto direction: probed true, declared false -> unavailable.
        avail, detail = cap.resolve(path=path, skip_probes=True)
        check("speakers" in avail, "a declared capability is granted")
        with open(path, "w") as f:
            f.write("capabilities:\n  rtc-wake: false\n")
        _avail, detail = cap.resolve(path=path, skip_probes=True)
        check(detail["rtc-wake"]["available"] is False,
              "a declaration can withhold a probed capability")

        # The direction that must NOT work.
        with open(path, "w") as f:
            f.write("capabilities:\n  device: true\n  sox: true\n")
        avail, detail = cap.resolve(path=path, skip_probes=True)
        check("device" not in avail and "sox" not in avail,
              "a declaration cannot grant a probed capability",
              str(sorted(avail)))

        with open(path, "w") as f:
            f.write("capabilities:\n  nonsense: true\n")
        _d, note = cap.load_declared(path)
        check(note is not None and "nonsense" in note,
              "an unknown capability name is reported, not silently obeyed")

        check(cap.load_declared(os.path.join(d, "absent.yaml")) == ({}, None),
              "an absent file grants nothing and is not an error")

    avail, _detail = cap.resolve(attended=False, skip_probes=True)
    check("human" not in avail, "--unattended withholds 'human'")
    avail, _detail = cap.resolve(attended=True, skip_probes=True)
    check("human" in avail, "and an attended run grants it")


def test_capability_gating():
    """Which cases run, which demote to manual, which block."""
    print("\ncapability gating")
    import runner

    semi = {"id": "X", "mode": "semi-automated", "level": "L3",
            "requires": ["device"]}
    check(runner.capability_gap(semi, {"device"}) == ["human"],
          "a semi-automated case needs a human without saying so",
          str(runner.capability_gap(semi, {"device"})))
    check(runner.capability_gap(semi, {"device", "human"}) == [],
          "and runs when one is there")

    auto = {"id": "X", "mode": "automated", "level": "L3",
            "requires": ["device", "usb-power"]}
    check(runner.capability_gap(auto, {"device"}) == ["usb-power"],
          "a missing capability is named")
    check(runner.capability_gap(auto, {"device", "human"}) == ["usb-power"],
          "an automated case does not need a human")

    build = {"id": "X", "mode": "automated", "level": "L1",
             "requires": ["kernel-tree"]}
    check(runner.capability_gap(build, set()) == [],
          "build levels are exempt -- they check for themselves")

    steps = {"steps": ["do a thing"]}
    check(runner.demotes_to_manual(steps, ["usb-power"]),
          "a missing actuator demotes to manual -- a person can unplug it")
    check(not runner.demotes_to_manual({}, ["usb-power"]),
          "unless nobody wrote down how")
    check(not runner.demotes_to_manual(steps, ["device"]),
          "a missing DEVICE never demotes: there is nothing to test by hand")
    check(not runner.demotes_to_manual(steps, ["usb-power", "device"]),
          "and one substitutable gap does not excuse an unsubstitutable one")


def test_results_roundtrip():
    print("\nresult records")
    run = results.Run(run_id="t", profile="smoke", target="x86_64-debug",
                      started=results.utc_iso())
    run.add(results.CaseResult(id="JT-PCM-002", status=results.PASS))
    run.add(results.CaseResult(id="JT-RATE-001", status=results.FAIL))
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "run.json")
        results.write(run, path)
        back = results.read(path)
    check(back["counts"] == {"pass": 1, "fail": 1}, "counts are derived",
          str(back.get("counts")))
    check(len(back["results"]) == 2, "results survive the round trip")


def test_case_streaming():
    """A case's progress reaches the terminal while it is still running.

    The failure this guards against is not a wrong answer but an unusable
    one: capture_output holds every byte until exit, so a case that
    power-cycles a device ten times was eighty seconds indistinguishable
    from a hang.

    The second half matters more. Once a case is chatty, the naive "first
    200 characters of stderr" reason reports its opening progress line as the
    cause of a failure that happened a minute later.
    """
    print("\ncase streaming")
    import runner

    script = ("import sys, time\n"
              "for i in (1, 2, 3):\n"
              "    print(f'cycle {i}/3', file=sys.stderr, flush=True)\n"
              "    time.sleep(0.05)\n"
              "print('artifact', flush=True)\n"
              "print('FAIL: cycle 3 broke', file=sys.stderr)\n"
              "print('cycle 3 broke', file=sys.stderr)\n"
              "sys.exit(1)\n")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "case.py")
        with open(path, "w") as f:
            f.write(script)

        seen = []
        rc, out, err = runner.stream_case(
            [sys.executable, path], dict(os.environ), 30,
            lambda ln, transient=False: seen.append(ln))
        check(rc == 1, "the exit status survives streaming", str(rc))
        check(seen and seen[0] == "cycle 1/3",
              "progress is delivered line by line as it arrives", str(seen[:1]))
        check(len(seen) == 5, "every stderr line is echoed", str(len(seen)))
        check(out.strip() == "artifact",
              "stdout stays separate and is captured, not echoed", repr(out))
        check("cycle 1/3" in err and "cycle 3 broke" in err,
              "and stderr is captured in full as well")

        check(runner.failure_reason(err, out, rc) == "cycle 3 broke",
              "the reason is the LAST line, not the first progress line",
              runner.failure_reason(err, out, rc))

        # THE ONE THAT MATTERS FOR INTERACTIVE CASES.
        #
        # Several lines written at once, then a long silence -- which is
        # exactly a case reporting progress and then asking a question. Every
        # line must arrive during the silence, not be held until the next
        # write. A buffered readline() passes the test above, where each line
        # is separated by a sleep, and fails this one: it returns the first
        # line and leaves the rest in a buffer that select() cannot see, so
        # the prompt stays invisible and the operator and the case wait for
        # each other. Observed on hardware before it was pinned here.
        with open(path, "w") as f:
            f.write("import sys, time\n"
                    "for i in (1, 2, 3):\n"
                    "    print(f'blink {i}/3 sent', file=sys.stderr)\n"
                    "print('?? did it blink? [y/n]', file=sys.stderr)\n"
                    "sys.stderr.flush()\n"
                    "time.sleep(3)\n")
        seen, times = [], []
        t0 = time.time()
        runner.stream_case([sys.executable, path], dict(os.environ), 30,
                           lambda ln, transient=False: (
                               seen.append(ln), times.append(time.time() - t0)))
        check(len(seen) == 4, "every line written in one burst is delivered",
              str(seen))
        check(seen and seen[-1].startswith("??"),
              "including the prompt, which is the last thing written",
              str(seen[-1:]))
        check(times and times[-1] < 2.0,
              "and it arrives while the case is still waiting, not after",
              f"prompt shown after {times[-1] if times else -1:.1f}s of a 3s wait")

        # A case that wedges must be killed at the deadline, not waited on.
        with open(path, "w") as f:
            f.write("import time\ntime.sleep(60)\n")
        t0 = time.time()
        rc, _out, err = runner.stream_case(
            [sys.executable, path], dict(os.environ), 1, None)
        check(rc == 124 and time.time() - t0 < 10,
              "a silent case is killed at its timeout", f"rc={rc}")
        check("timed out" in err, "and says so", err)

    check(runner.mark(results.PASS) != runner.mark(results.FAIL),
          "pass and fail are visually distinct at a glance")


def test_terminal():
    """Styling is applied on the way to the screen and nowhere else.

    The property worth defending is that no escape sequence ever reaches a
    file. A coloured stderr.txt is unreadable a year later and defeats grep,
    so cases emit plain text and only the runner paints it.
    """
    print("\nterminal styling")
    from lib import term

    plain, painted = term.Style(enabled=False), term.Style(enabled=True)
    check(plain("hello", "bold", "red") == "hello",
          "disabled, styling is the identity function")
    check("\033[" in painted("hello", "bold", "red"),
          "enabled, it emits SGR codes")

    saved = os.environ.get("NO_COLOR")
    try:
        os.environ["NO_COLOR"] = "1"
        check(not term.supported(io.StringIO()), "NO_COLOR is honoured")
    finally:
        if saved is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = saved
    check(not term.supported(io.StringIO()),
          "and a non-terminal is never painted")

    check(term.visible_len(painted("abc", "bold", "red")) == 3,
          "a styled string is measured by the columns it occupies, not bytes",
          str(term.visible_len(painted("abc", "bold", "red"))))

    check(term.decorate(plain, ">> do a thing") == "▶ do a thing",
          "an instruction is marked by the prefix the case wrote",
          term.decorate(plain, ">> do a thing"))
    check(term.decorate(plain, "?? really?") == "◆ really?",
          "and so is a question")

    # A transient line redraws in place on a terminal...
    out = io.StringIO()
    live = term.Live(out=out, enabled=True, indent="  ")
    live.write("counting 3", transient=True)
    live.write("counting 2", transient=True)
    live.write("done", transient=False)
    text = out.getvalue()
    check(text.count("\n") == 1,
          "on a terminal, only the line that stays gets a newline", repr(text))
    check(text.endswith("  done\n"), "and the last word is the one that stays",
          repr(text[-20:]))

    # ...and becomes ordinary lines anywhere else, because a log that
    # overwrote itself is a log that lost its history.
    out = io.StringIO()
    live = term.Live(out=out, enabled=False, indent="  ")
    live.write("counting 3", transient=True)
    live.write("counting 2", transient=True)
    live.write("done", transient=False)
    check(out.getvalue() == "  counting 3\n  counting 2\n  done\n",
          "redirected, every update is kept as its own line",
          repr(out.getvalue()))


def test_operator_prompts():
    """Asking a person something, without deadlocking on them.

    The prompt must end in a newline. The runner streams stderr with
    readline(), so a prompt written without one is held in the pipe forever:
    the operator waits for a question that never appears while the case waits
    for an answer to a question never asked. It is invisible in isolation and
    only shows up on hardware, which is exactly why it is pinned here.
    """
    print("\noperator prompts")
    from lib.case import Case

    saved_in, saved_err = sys.stdin, sys.stderr
    saved_env = os.environ.get("JT_ATTENDED")
    try:
        os.environ["JT_ATTENDED"] = "1"
        r, w = os.pipe()
        os.write(w, b"y\n")
        os.close(w)
        sys.stdin = os.fdopen(r)
        sys.stderr = io.StringIO()

        c = Case()
        answer = c.confirm("did the LEDs blink?", timeout=5)
        written = sys.stderr.getvalue()
        sys.stderr = saved_err

        check(answer is True, "a yes is read back as True", str(answer))
        check(written.endswith("\n"),
              "every byte written to the operator ends a line -- an "
              "unterminated prompt never leaves the pipe", repr(written[-30:]))
        check("did the LEDs blink?" in written,
              "and the question itself reaches them", repr(written))

        # The answer is evidence and belongs in the record, not only on screen.
        check(any("did the LEDs blink? -> y" in n for n in c._note),
              "the answer is recorded, not just displayed", str(c._note))

        # Unattended, asking must return at once rather than wait for a person
        # who is not there -- the runner normally prevents this, but a case
        # reached out of band must not hang the machine.
        os.environ["JT_ATTENDED"] = "0"
        sys.stderr = io.StringIO()
        t0 = time.time()
        c2 = Case()
        unattended = c2.confirm("anybody there?", timeout=30)
        sys.stderr = saved_err
        check(unattended is None and time.time() - t0 < 1.0,
              "unattended, a question is not asked and does not block",
              f"{unattended} after {time.time() - t0:.1f}s")
    finally:
        sys.stdin, sys.stderr = saved_in, saved_err
        if saved_env is None:
            os.environ.pop("JT_ATTENDED", None)
        else:
            os.environ["JT_ATTENDED"] = saved_env


def test_semi_automated_routing():
    """A semi-automated case runs; without an operator it defers."""
    print("\nsemi-automated routing")
    import runner

    check("semi-automated" in runner.RUNNABLE_MODES,
          "the runner executes semi-automated cases rather than filing them "
          "with the manual ones")

    case = {"id": "X", "mode": "semi-automated", "level": "L3",
            "requires": ["device"], "steps": ["do the thing"]}
    attended = runner.capability_gap(case, {"device", "human"})
    check(attended == [], "with an operator present it simply runs",
          str(attended))

    gap = runner.capability_gap(case, {"device"})
    check(gap == ["human"], "without one, the gap is the person", str(gap))
    check(runner.demotes_to_manual(case, gap),
          "which DEFERS to the checklist rather than blocking -- an "
          "unattended run postpones coverage, it does not lack it")

    check(not runner.demotes_to_manual({"mode": "semi-automated"}, ["human"]),
          "unless the case never wrote down its manual form")


def test_manual_fallback():
    """A case demoted to manual must reach the checklist intact.

    The parameters are the part that rots silently. checklist.py renders what
    the RUN RECORD says, so if the runner records a pending case without its
    resolved parameters, every per-target override is quietly undone -- the
    operator on a Pi 1B is handed the catalog's four sample rates when the
    profile deliberately says two, and nothing anywhere reports a problem.
    """
    print("\nmanual fallback")
    import checklist
    catalog = load("catalog.yaml")
    cases = {c["id"]: c for c in catalog["cases"]}

    with tempfile.TemporaryDirectory() as d:
        run = {"results": [
            {"id": "JT-AUDIO-001", "status": results.PENDING, "mode": "manual",
             "params": {"rates": [44100]},
             "reason": "manual -- answer via checklist.py"},
            {"id": "JT-PROBE-003", "status": results.PENDING, "mode": "manual",
             "params": {"iterations_per_run": 3},
             "reason": "no usb-power -- do it by hand via checklist.py"},
            {"id": "JT-PCM-002", "status": results.PASS, "mode": "automated",
             "params": {}},
            {"id": "JT-AUDIO-002", "status": results.BLOCKED,
             "mode": "automated", "params": {}},
        ]}
        with open(os.path.join(d, "run.json"), "w") as f:
            f.write(json.dumps(run))

        items = checklist.pending_cases(d, cases)
        ids = [c["id"] for c, _p, _r in items]
        check(ids == ["JT-AUDIO-001", "JT-PROBE-003"],
              "only pending cases reach the checklist", str(ids))
        check(all(s != "JT-AUDIO-002" for s in ids),
              "a blocked case is not offered -- it cannot be done here at all")

        params = {c["id"]: p for c, p, _r in items}
        check(params["JT-PROBE-003"] == {"iterations_per_run": 3},
              "the RESOLVED parameters are rendered, not the catalog defaults",
              str(params["JT-PROBE-003"]))

        md = checklist.render("functional", "armhf-prod", items, "t/x")
        demoted = "JT-PROBE-003 -- " in md and "Automated form could not run" in md
        check(demoted, "a demoted case says why it is being done by hand")
        after = md.split("## JT-AUDIO-001")[1].split("## ")[0]
        check("Automated form could not run" not in after,
              "a case that is manual by nature does not claim to be a fallback")


def test_run_resolution():
    """Finding the run a manual checklist belongs to.

    The failure this guards against is quiet: answers imported into the wrong
    run, or into a run from last week, look exactly like answers imported
    correctly.
    """
    print("\nrun resolution")
    import checklist
    with tempfile.TemporaryDirectory() as d:
        base = os.path.join(d, "x86_64-debug")
        os.makedirs(base)
        for stamp in ("20260808T100000Z-smoke", "20260809T100000Z-smoke",
                      "20260809T110000Z-functional"):
            os.makedirs(os.path.join(base, stamp))
            with open(os.path.join(base, stamp, "run.json"), "w") as f:
                f.write("{}")
        os.makedirs(os.path.join(base, "not-a-run"))

        p, _age = results.find_latest_run(d, "x86_64-debug", "smoke")
        check(os.path.basename(p) == "20260809T100000Z-smoke",
              "the newest run for the profile wins", str(p))
        p2, _ = results.find_latest_run(d, "x86_64-debug", "functional")
        check(os.path.basename(p2) == "20260809T110000Z-functional",
              "profiles do not bleed into each other")
        check(results.find_latest_run(d, "x86_64-debug", "soak") == (None, None),
              "an absent profile resolves to nothing, not to something else")
        check(results.parse_run_dir("not-a-run") is None,
              "a directory that is not a run is ignored")

        # Age is computed from the stamp, not from mtime: these directories
        # were created seconds ago but claim to be from 2026-08-08.
        old, age = results.find_latest_run(d, "x86_64-debug", "functional")
        check(age is not None and age >= 0, "age comes from the run stamp",
              str(age))

    # A rendered checklist must name its run, and that name must survive being
    # read back -- otherwise --import silently falls back to guessing.
    md = checklist.render("smoke", "x86_64-debug",
                          [({"id": "JT-MIDI-002", "title": "t", "level": "L3",
                             "area": "MIDI", "mode": "manual",
                             "steps": ["do a thing"], "pass": "it works"},
                            {}, "")],
                          "x86_64-debug/20260809T182522Z-smoke")
    m = checklist.RUN_RE.search(md)
    check(m and m.group("path") == "x86_64-debug/20260809T182522Z-smoke",
          "a rendered checklist carries the run it belongs to",
          str(m and m.group("path")))
    check(checklist.ANSWER_RE.search(md) is not None,
          "and still carries a machine-readable answer line")


def test_privilege_boundary():
    """The helper is the whole privileged surface, so drift in it is silent.

    None of this needs root: it compares the repository's helper against the
    Python that drives it. The failure these catch is a verb renamed on one
    side only, which shows up at runtime as a case that is blocked or, worse,
    as log slicing that quietly stops working.
    """
    print("\nprivilege boundary")
    helper = os.path.join(HERE, "priv", "jockey3-testctl")
    check(os.path.exists(helper), "the helper exists in the repository")
    if not os.path.exists(helper):
        return
    src = open(helper, encoding="utf-8").read()

    rc = subprocess.run(["bash", "-n", helper], capture_output=True)
    check(rc.returncode == 0, "the helper is syntactically valid",
          rc.stderr.decode()[:120])

    # Verbs the script implements, taken from the case labels of its dispatch.
    implemented = set(re.findall(r"^([a-z][a-z-]*)\)$", src, re.M))
    driver_src = "".join(
        open(os.path.join(HERE, "lib", f), encoding="utf-8").read()
        for f in ("priv.py", "kmsg.py"))
    # call("verb", ...) and the streaming route, verb_argv("verb").
    called = set(re.findall(r'(?:call|verb_argv)\(\s*"([a-z][a-z-]*)"', driver_src))
    check(called <= implemented, "every verb the framework invokes is implemented",
          f"missing: {sorted(called - implemented)}")

    # The marker token is generated in kmsg.py and validated in the helper.
    # If those two drift, markers are silently rejected and every case gets
    # classified against the entire boot log instead of its own slice.
    pat = re.search(r"=~ (\^JT-MARK.*?) \]\]", src)
    check(pat is not None, "the helper validates the marker token")
    if pat:
        token = kmsg.Marker("JT-PROBE-001#1").token
        rx = pat.group(1).replace("\\ ", " ")
        check(re.match(rx, token) is not None,
              "a generated marker token passes the helper's validation",
              f"token={token!r} pattern={rx!r}")

    # Nothing outside priv.py may reach for root on its own.
    strays = []
    for sub in ("lib", "cases"):
        for name in sorted(os.listdir(os.path.join(HERE, sub))):
            if not name.endswith(".py") or name == "priv.py":
                continue
            body = open(os.path.join(HERE, sub, name), encoding="utf-8").read()
            if re.search(r'["\[]\s*"sudo"|geteuid\(\)\s*!=\s*0', body):
                strays.append(f"{sub}/{name}")
    check(not strays, "only priv.py reaches for privilege", str(strays))

    # A local read before it is assigned. Python only notices at run time, and
    # a dry run never executes a case body -- so JT-AUDIO-005 shipped with its
    # actuator chosen from a `settle` that was parsed three lines later, and
    # the first anyone knew was an UnboundLocalError on the bench.
    #
    # Deliberately conservative: names bound by a loop or comprehension are
    # skipped, since those legitimately read what an earlier iteration bound.
    # It catches straight-line ordering mistakes, which is the one that bit.
    early = []
    for sub in ("lib", "cases", "actions", "."):
        base = os.path.join(HERE, sub)
        for name in sorted(os.listdir(base)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read(), path)
            except SyntaxError as e:
                early.append(f"{sub}/{name}: {e}")
                continue
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                bound, loopy = {}, set()
                for node in ast.walk(fn):
                    if isinstance(node, (ast.For, ast.While)):
                        for sub2 in ast.walk(node):
                            if (isinstance(sub2, ast.Name)
                                    and isinstance(sub2.ctx, ast.Store)):
                                loopy.add(sub2.id)
                    if isinstance(node, ast.comprehension):
                        for sub2 in ast.walk(node.target):
                            if isinstance(sub2, ast.Name):
                                loopy.add(sub2.id)
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                        bound[node.id] = min(bound.get(node.id, node.lineno),
                                             node.lineno)
                args = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
                if fn.args.vararg:
                    args.add(fn.args.vararg.arg)
                if fn.args.kwarg:
                    args.add(fn.args.kwarg.arg)
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Name)
                            and isinstance(node.ctx, ast.Load)
                            and node.id in bound and node.id not in args
                            and node.id not in loopy
                            and node.lineno < bound[node.id]):
                        early.append(f"{sub}/{name}:{node.lineno}: "
                                     f"{fn.name}() reads '{node.id}' before "
                                     f"line {bound[node.id]} assigns it")
    check(not early, "no local is read before it is assigned",
          "; ".join(early[:4]))


# What the stubbed streams pretend the hardware is doing. Set by the fake
# aplay/arecord, read by the fake watcher when it is stopped -- which is the
# real order of events, so the fake trace describes the stream that just ran.
CLOCK = {"rate": 48000, "scale": 1.0, "plateau": 0.0}


class FakeWatch:
    """A watch_pcm that synthesizes a hw_ptr trace instead of reading /proc.

    Times are real monotonic readings offset arithmetically, not slept
    through: the point is to exercise the measurement, not to spend four
    seconds per change proving that time passes.
    """

    def __init__(self, index, pcm="pcm0p", sub="sub0", interval=0.02):
        self.pcm, self.interval = pcm, interval
        self.trace, self.xruns, self.avail_max = [], 0, 0
        self.hw_params = {}
        self.state = None
        self._t0 = None

    def start(self):
        self._t0 = time.monotonic()
        self.state = "RUNNING"

    def stop(self):
        self.hw_params = {"buffer_size": 4096, "period_size": 1024,
                          "rate": CLOCK["rate"]}
        hz = CLOCK["rate"] * CLOCK["scale"]
        startup, duration = 0.30, 4.0
        ptr, t = 0, self._t0 + startup
        end = self._t0 + startup + duration
        # A plateau in the middle, when asked for: the pointer stops and then
        # resumes, which is what a stall looks like from here.
        hold_at = self._t0 + startup + 1.8
        while t <= end:
            self.trace.append((t, ptr))
            if not (CLOCK["plateau"] and
                    hold_at <= t < hold_at + CLOCK["plateau"]):
                ptr += int(hz * self.interval)
            t += self.interval
        return self


def test_rate_change_case_runs():
    """Execute JT-RATE-001 end to end with the hardware stubbed.

    Every other test here inspects data or helpers; none of them ever runs a
    case, and that gap has a cost. JT-RATE-001 once completed a full 31-second
    hardware sweep and then died in its metric loop on "too many values to
    unpack" -- a record had grown from five fields to seven and one of the
    three places unpacking it was missed. No hardware is needed to catch that.

    Playback and capture are replaced by stubs, so this exercises the loop, the
    classification and the metric emission, not ALSA.
    """
    print("\nrate-change case, hardware stubbed")
    import types, tempfile
    spec = importlib.util.spec_from_file_location(
        "rate_change_case", os.path.join("cases", "rate_change.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["rate_change_case"] = m
    spec.loader.exec_module(m)

    frame = m.CAPTURE_CHANNELS * m.BYTES_PER_SAMPLE
    workdir = tempfile.mkdtemp()

    class FakeCase:
        id = "JT-RATE-001"
        attended = False
        device = "hw:9,0"
        card = 9

        def __init__(self, params):
            self.params = params
            self.workdir = workdir
            self.metrics = {}
            self.fails = []
            self.notes = []

        def require_card(self): pass
        def require_tools(self, *a): pass
        def metric(self, k, v): self.metrics[k] = v
        def fail(self, t): self.fails.append(t)
        def note(self, t): self.notes.append(t)
        def progress(self, t): pass
        def status(self, t): pass
        def blocked(self, t): raise AssertionError("blocked: " + t)
        def done(self): pass

    def run(params, mode):
        case = FakeCase(params)
        m.Case = lambda: case
        m.alsa = types.SimpleNamespace(
            xruns=lambda *a: 0,
            device_name=lambda *a: "hw:9,0",
            wait_for_card_live=lambda *a, **k: mode != "dead_device")
        # A kernel log that carries each change's marker, plus a capture stall
        # and a capture-triggered reset after the marker for change 3. The
        # case must attribute those to change 3 and to no other.
        log = []

        def make_marker(label):
            token = f"JT-MARK {label} deadbeef"
            log.append(f"[100.0] {token}")
            if label.endswith("#change3-88200"):
                log.append("[100.1] snd-reloop-jockey3 1-2.2:1.0: "
                           "Capture URB has stalled.")
                log.append("[100.15] snd-reloop-jockey3 1-2.2:1.0: Rate "
                           "change to 88200 Hz left a stream stalled "
                           "(playback_alive=1, capture_alive=0, "
                           "capture_open=1); attempting recovery")
                log.append("[100.2] snd-reloop-jockey3 1-2.2:1.0: Capture "
                           "stream stalled (rate change); restarting URBs "
                           "to recover")
                log.append("[100.3] snd-reloop-jockey3 1-2.2:1.0: Capture "
                           "stream still stalled after URB restart; queuing "
                           "full USB reset (rate change)")
            return types.SimpleNamespace(token=token, written=True,
                                         write=lambda: True)

        m.kmsg = types.SimpleNamespace(Marker=make_marker,
                                       read_log=lambda: log)
        m.alsa.substreams = lambda i: {"playback": ["pcm0p"],
                                       "capture": ["pcm0c"], "rawmidi": []}
        # Configured immediately, so wait_configured() returns at once and the
        # rate_change_stream ordering is exercised without any real sleeping.
        m.alsa.pcm_status = lambda i, p, *a: {"state": "RUNNING"}
        m.alsa.pointer_rate = alsa.pointer_rate
        m.alsa.watch_pcm = FakeWatch
        def fake_play(dev, rate, sec):
            CLOCK["rate"] = rate
            return (None, None)

        m.start_playback = fake_play
        m.finish_playback = lambda p, g, sec, t0: (0, "", sec * 1.02)

        class Rec:
            returncode = 0

            def __init__(self, path, rate, sec):
                self.path, self.rate, self.sec = path, rate, sec

            def communicate(self, timeout=None):
                if mode == "live":
                    with open(self.path, "wb") as f:
                        f.write(bytes([(i * 7) % 256 for i in
                                       range(frame * int(self.rate * self.sec))]))
                elif mode == "nodata":
                    open(self.path, "wb").close()
                self.returncode = 1 if mode == "error" else 0
                return ("", "arecord: open failed" if mode == "error" else "")

            def kill(self): pass

        def fake_rec(dev, rate, sec, path):
            CLOCK["rate"] = rate
            return Rec(path, rate, sec)

        m.start_capture = fake_rec
        m.main()
        return case

    healthy = run({"iterations_per_run": 2, "seconds_per_rate": 1}, "live")
    check(not healthy.fails, "a healthy sweep produces no failures",
          str(healthy.fails[:1]))
    check(healthy.metrics.get("capture_live_changes") == 8,
          "every change is counted as live",
          str(healthy.metrics.get("capture_live_changes")))

    nodata = run({"iterations_per_run": 1, "seconds_per_rate": 1}, "nodata")
    check(nodata.metrics.get("capture_nodata_changes") == 4,
          "an empty capture counts as nodata, not silence",
          str(nodata.metrics))
    check(nodata.metrics.get("capture_silent_changes") == 0,
          "and never as silence -- they are different faults")
    check(all("no samples" in f for f in nodata.fails),
          "the failure says no samples, not a dead converter",
          str(nodata.fails[:1]))

    err = run({"iterations_per_run": 1, "seconds_per_rate": 1}, "error")
    check(err.metrics.get("capture_error_changes") == 4,
          "a failing arecord is its own outcome", str(err.metrics))

    short = run({"iterations_per_run": 1, "seconds_per_rate": 1}, "live")
    check(short.metrics.get("timing_check_enforced") is False,
          "the timing check is not enforced at 1 s")
    long_run = run({"iterations_per_run": 1, "seconds_per_rate": 5}, "live")
    check(long_run.metrics.get("timing_check_enforced") is True,
          "and is enforced at 5 s")

    # A card that never comes back must stop the sweep instead of retrying
    # against nothing for the rest of the run -- see alsa.wait_for_card_live().
    dead = run({"iterations_per_run": 5, "seconds_per_rate": 1}, "dead_device")
    check(dead.metrics.get("aborted_device_unavailable") is True,
          "a dead card aborts the sweep and says so",
          str(dead.metrics.get("aborted_device_unavailable")))
    check(dead.metrics.get("rate_changes") == 1,
          "aborting on the first change costs exactly one change, not the "
          "whole run", str(dead.metrics.get("rate_changes")))
    check(any("did not come back" in f for f in dead.fails),
          "the failure names the reason, not just a symptom",
          str(dead.fails[:1]))

    # 44.1 against 48 kHz is 8.1%. The elapsed-time check could not resolve it
    # at a 20% tolerance and the case warned about it on every run; the
    # steady-state measurement at 5% sees it plainly. The blind-spot report
    # follows the tolerance that is actually enforced, so it now says nothing.
    seen = run({"iterations_per_run": 1, "seconds_per_rate": 1,
                "rates": [44100, 48000]}, "live")
    check(seen.metrics.get("rate_check_blind_steps") == 0,
          "a 44.1/48 sweep is resolvable at the 5% steady-state tolerance",
          str(seen.metrics.get("rate_check_blind_steps")))
    blind = run({"iterations_per_run": 1, "seconds_per_rate": 1,
                 "rates": [44100, 48000], "steady_tolerance": 0.20}, "live")
    check(blind.metrics.get("rate_check_blind_steps", 0) > 0,
          "and is reported as unresolvable again if the tolerance is widened",
          str(blind.metrics.get("rate_check_blind_steps")))

    # The measurement itself, through the case: a device clocking at half the
    # requested rate must be caught, and a stall must not be reported as one.
    steady_ok = run({"iterations_per_run": 1, "seconds_per_rate": 4}, "live")
    check(not steady_ok.fails, "a correct clock passes the steady-state check",
          str(steady_ok.fails[:1]))
    check(steady_ok.metrics.get("steady_error_capture_pct") is not None
          and steady_ok.metrics["steady_error_capture_pct"] < 5.0,
          "and the measured error is inside the 5% target",
          str(steady_ok.metrics.get("steady_error_capture_pct")))
    check(steady_ok.metrics.get("startup_s_capture_max") is not None,
          "the excluded start-up cost is measured, not assumed",
          str(steady_ok.metrics.get("startup_s_capture_max")))

    CLOCK["scale"] = 0.5
    try:
        halved = run({"iterations_per_run": 1, "seconds_per_rate": 4}, "live")
    finally:
        CLOCK["scale"] = 1.0
    check(any("not " in f and "Hz" in f for f in halved.fails),
          "a device clocking at half the requested rate fails",
          str(halved.fails[:1]))

    CLOCK["plateau"] = 0.5
    try:
        stalled = run({"iterations_per_run": 1, "seconds_per_rate": 4}, "live")
    finally:
        CLOCK["plateau"] = 0.0
    check(stalled.metrics.get("pointer_plateau_changes_capture") == 4,
          "a pointer that stops and resumes is counted as a stall",
          str(stalled.metrics.get("pointer_plateau_changes_capture")))
    check(not stalled.fails,
          "and is NOT reported as a wrong clock -- the rate is measured "
          "around the plateau, not through it", str(stalled.fails[:1]))

    # Stalls are attributed to the change that caused them, not just totalled.
    attr = run({"iterations_per_run": 1, "seconds_per_rate": 1}, "live")
    check(attr.metrics.get("capture_stall_hw_params_total") == 1,
          "a capture stall in the log is counted",
          str(attr.metrics.get("capture_stall_hw_params_total")))
    check(attr.metrics.get("reset_on_rate_change_total") == 1,
          "and so is the reset it caused",
          str(attr.metrics.get("reset_on_rate_change_total")))
    check(attr.metrics.get("capture_stall_hw_params_change_3_88200") == 1,
          "against the specific change that produced it",
          str({k: v for k, v in attr.metrics.items() if "_change_" in k}))
    check(attr.metrics.get("branch_change_3_88200") == "reset",
          "and the change is recorded as having taken the reset branch",
          str(attr.metrics.get("branch_change_3_88200")))
    check(attr.metrics.get("branch_reset") == 1
          and attr.metrics.get("branch_clean") == 3,
          "with the other three changes recorded as clean",
          str({k: v for k, v in attr.metrics.items()
               if k.startswith("branch_") and "_change_" not in k}))
    check(attr.metrics.get("stalls_direction_up") == 1
          and attr.metrics.get("changes_direction_up") == 1,
          "grouped by direction: 88200 after 44100 is an upward step",
          str({k: v for k, v in attr.metrics.items()
               if "_direction_" in k}))
    check(attr.metrics.get("stalls_pair_44100_to_88200") == 1
          and attr.metrics.get("changes_pair_44100_to_88200") == 1,
          "and by the specific transition, as numbers ledger.py can trend",
          str({k: v for k, v in attr.metrics.items() if "_pair_" in k}))
    check(attr.metrics.get("transition_change_1") == "start->96000",
          "the first change has no predecessor and does not borrow one",
          str(attr.metrics.get("transition_change_1")))

    # The key metric: how often a rate change costs a device reset. The fake
    # log carries one stall and one reset over four changes.
    check(attr.metrics.get("resets_per_change_pct") == 25.0,
          "resets per rate change is reported as a percentage of changes",
          str(attr.metrics.get("resets_per_change_pct")))
    check(attr.metrics.get("resets_total_device") == 1,
          "with the reset count it came from",
          str(attr.metrics.get("resets_total_device")))
    check(attr.metrics.get("stalls_per_change_pct") == 25.0,
          "beside the stall rate, so the two can move independently",
          str(attr.metrics.get("stalls_per_change_pct")))
    # It must NOT come from the per-change attribution: markers can fail to
    # land, and on 2026-08-15 that turned a real 30% into a headline of zero.
    check(attr.metrics.get("attribution_trustworthy") is not None,
          "and the run says whether its per-change attribution can be trusted")

    # The arm being run has to be recorded, or a comparison between arms is
    # comparing two things it cannot name.
    pb = run({"iterations_per_run": 1, "seconds_per_rate": 1,
              "rate_change_stream": "playback"}, "live")
    check(pb.metrics.get("rate_change_stream") == "playback",
          "the stream that performs the change is recorded")
    check(pb.metrics.get("lead_stream_configure_timeouts") == 0,
          "and the leading stream reached hw_params on every change",
          str(pb.metrics.get("lead_stream_configure_timeouts")))
    noc = run({"iterations_per_run": 1, "seconds_per_rate": 1,
               "capture": False}, "live")
    check(noc.metrics.get("rate_change_stream") == "playback",
          "capture=false forces the change onto playback and says so",
          str(noc.metrics.get("rate_change_stream")))
    check(any("re-detected" in n for n in noc.notes),
          "and warns that its stall count is not per-change incidence",
          str(noc.notes))


def test_rate_change_log_attribution():
    """The several call sites that share one stall message, told apart.

    jockey3_wait_urb_stream_started() logs the identical "Capture URB has
    stalled." from hw_params(), from prepare(), and twice inside
    jockey3_recover_urb_stream() -- which, since Stage 3, is called from both.
    Counting the string counts neither stalls nor changes, so the case
    resolves each one by the context line that follows it. This is where that
    resolution is checked, because getting it wrong silently inflates the one
    number milestone 13 is judged on.
    """
    print("\nrate-change log attribution")
    spec = importlib.util.spec_from_file_location(
        "rate_change_case2", os.path.join("cases", "rate_change.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    def names(body):
        return [n for n, _ in m.classify_events([DRIVER + s for s in body])]

    # The deferred branch, in full: hw_params stalls and says nothing further
    # (its explanation is dev_dbg), the next capture open stalls again and
    # announces itself, the URB restart fails, the full reset fails.
    seq = names([
        "Capture URB has stalled.",
        "Capture URB has stalled.",
        "Capture stream stalled (opening a capture stream); restarting URBs "
        "to recover",
        "Capture URB has stalled.",
        "Capture stream still stalled after URB restart; queuing full USB "
        "reset (opening a capture stream)",
        "Capture URB has stalled.",
        "Capture stream still stalled after full USB reset; hardware may need "
        "power-cycling (opening a capture stream)",
    ])
    check(seq.count("capture_stall_hw_params") == 1,
          "the rate change's own stall is counted exactly once",
          str(seq))
    check(seq.count("capture_stall_on_open") == 1,
          "the one from the capture open is not confused with it", str(seq))
    check(seq.count("capture_stall_after_urb_restart") == 1
          and seq.count("capture_stall_after_reset") == 1,
          "and the two recovery retries are their own events", str(seq))

    # The reset branch, from hw_params() itself: a stall, the light retry
    # announcement, then the reset line. Nothing else follows, so the first
    # stall must still land on hw_params.
    seq = names([
        "Capture URB has stalled.",
        "Rate change to 44100 Hz left a stream stalled (playback_alive=1, "
        "capture_alive=0, capture_open=1); attempting recovery",
        "Capture stream stalled (rate change); restarting URBs to recover",
        "Capture stream still stalled after URB restart; queuing full USB "
        "reset (rate change)",
    ])
    check(seq == ["capture_stall_hw_params", "hw_params_light_retry",
                  "reset_on_rate_change"],
          "a stall followed by the reset line is the hw_params one", str(seq))

    # A playback stall must never be absorbed by a capture context line.
    seq = names([
        "Playback URB has stalled.",
        "Capture URB has stalled.",
        "Capture stream stalled (opening a capture stream); restarting URBs "
        "to recover",
    ])
    check(seq.count("playback_stall") == 1
          and seq.count("capture_stall_on_open") == 1
          and "capture_stall_hw_params" not in seq,
          "playback and capture stalls are resolved independently", str(seq))

    # Stage 3's new playback-at-prepare path: previously this branch only
    # logged and never recovered, so it could never reach an escalation or
    # give-up line at all. Now it can, and each gets its own event name.
    seq = names([
        "Playback URB has stalled.",
        "Playback stream stalled (preparing a playback stream); restarting "
        "URBs to recover",
        "Playback URB has stalled.",
        "Playback stream still stalled after URB restart; queuing full USB "
        "reset (preparing a playback stream)",
    ])
    check(seq.count("prepare_playback") == 1
          and seq.count("reset_after_playback_prepare") == 1,
          "the playback-prepare light retry and its escalation are their "
          "own events", str(seq))

    # The chip-wide recovery budget can give up without ever reaching a reset.
    seq = names([
        "Capture URB has stalled.",
        "Capture stream stalled (opening a capture stream); restarting URBs "
        "to recover",
        "Capture stream still stalled after URB restart; recovery budget "
        "exhausted, not resetting (opening a capture stream)",
    ])
    check(seq.count("recovery_budget_exhausted") == 1,
          "a budget-exhausted give-up is its own event", str(seq))

    # A stall with no context at all -- the playback-only run, where the
    # deferred branch is the end of the story.
    check(names(["Capture URB has stalled."]) == ["capture_stall_hw_params"],
          "an unexplained stall belongs to the rate change")

    # Windows: a reset completing after the streams closed belongs to the gap,
    # not to whatever change runs next.
    class Mark:
        def __init__(self, token):
            self.token, self.written = token, True

    marks = [("change", 1, Mark("MARK-C1")), ("gap", 1, Mark("MARK-G1")),
             ("change", 2, Mark("MARK-C2"))]
    lines = ["MARK-C1", "during one", "MARK-G1", "after one closed", "MARK-C2",
             "during two"]
    win = {(k, n): body for k, n, body in m.window_lines(lines, marks)}
    check(win[("change", 1)] == ["during one"],
          "the change window ends when its streams close", str(win))
    check(win[("gap", 1)] == ["after one closed"],
          "and the late line is charged to the gap, not to the next change",
          str(win))


def test_marker_labels():
    """A marker the privileged helper will reject is a marker that never lands.

    The helper validates the token it is handed and rejects anything outside
    its charset; priv.dmesg_mark() then returns False and Marker.write()
    carries on, because a missing marker is not fatal. It is not fatal, but it
    is not harmless either: JT-RATE-001 wrote "#change3@88200", every start
    marker was rejected while every end marker got through, and each window
    shifted by one change. The case reported "capture stalled on 0/20 changes"
    beside six resets and passed.

    The charset here is checked against the helper's own regex, read from the
    script, so the two cannot drift apart silently.
    """
    print("\nkernel-log marker labels")
    helper = os.path.join(HERE, "priv", "jockey3-testctl")
    with open(helper, "r", encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"\^JT-MARK\\ \[([^\]]+)\]\{1,64\}", src)
    check(m is not None, "the helper's marker regex is still findable")
    if m:
        check(m.group(1) == "A-Za-z0-9._:#+-",
              "and lib/kmsg.py's LABEL_OK matches it", m.group(1))

    mk = kmsg.Marker("JT-RATE-001#change3@88200")
    check("@" not in mk.token, "a label with '@' is sanitized, not rejected",
          mk.token)
    check(re.match(r"^JT-MARK [A-Za-z0-9._:#+-]{1,64} [0-9a-f]{8,32}$",
                   mk.token) is not None,
          "and the resulting token passes the helper's own validation",
          mk.token)
    check(kmsg.Marker("plain#gap12").label == "plain#gap12",
          "while a label that was already fine is left alone")
    check(len(kmsg.Marker("x" * 200).label) <= 64,
          "and an over-long label is truncated to what the helper accepts")


def test_run_log_trimming():
    """dmesg.txt must cover this run, and say so when it cannot.

    The kernel ring buffer on the test machines holds roughly eighteen hours,
    so an untrimmed capture is mostly earlier runs. Two JT-RATE-001 runs on
    2026-08-17 that stalled zero times in 244 changes each shipped a dmesg.txt
    containing 62 and 10 "Capture URB has stalled." lines respectively, every
    one of them from a previous run. Read raw, either file argues the opposite
    of what the run actually measured.

    The fallback matters as much as the trim: when the run-start marker never
    lands, the whole log is kept rather than nothing, because too much context
    can be filtered later and too little cannot be recovered. What must never
    happen is an untrimmed file that reads as though it were trimmed.
    """
    print("\nrun-scoped dmesg capture")

    mk = kmsg.Marker("run#20260817T161831Z")
    mk.written = True
    old = ["[100.0] snd-reloop-jockey3 1-2.2:1.0: Capture URB has stalled.",
           "[101.0] usb 1-2.2: reset high-speed USB device number 9"]
    lines = old + [f"[200.0] {mk.token}",
                   "[201.0] JT-MARK JT-RATE-001#change1-96000 abc123def456",
                   "[202.0] snd-reloop-jockey3 1-2.2:1.0: Rate set OK"]

    text, trimmed = kmsg.run_log(lines, mk)
    check(trimmed is True, "a written marker trims the log")
    check("Capture URB has stalled" not in text,
          "and an earlier run's stall line does not survive the trim")
    check("Rate set OK" in text, "while this run's own lines do")

    # read_log() runs `dmesg --raw` for the classifier, but the saved artifact
    # drops the "<N>" priority prefix -- the "[time] message" body is what a
    # human reads, and it is unchanged from before --raw.
    raw = [f"[300.0] {mk.token}",
           "<7>[301.0] snd-reloop-jockey3 1-2.2:1.0: Starting all URBs (warm start)",
           "<4>[302.0] snd-reloop-jockey3 1-2.2:1.0: Playback URB has stalled."]
    rtext, _ = kmsg.run_log(raw, mk)
    check("<7>" not in rtext and "<4>" not in rtext,
          "the saved log drops the raw syslog-priority prefix")
    check("[301.0] snd-reloop-jockey3" in rtext,
          "while the timestamp and message are untouched")
    # Three, not two: slice_since() consumes the marker line as well as the
    # two that preceded it.
    check("3 earlier line(s) trimmed" in text,
          "and the header counts what it dropped, marker line included")

    unwritten = kmsg.Marker("run#never-landed")
    unwritten.written = False
    text, trimmed = kmsg.run_log(lines, unwritten)
    check(trimmed is False, "an unwritten marker keeps the whole buffer")
    check("Capture URB has stalled" in text,
          "so no context is lost when the marker fails")
    check("WHOLE kernel ring buffer" in text.split("\n")[0],
          "but the very first line says so, loudly")

    # The marker is written before any case runs, so a marker that is written
    # but absent from the log (buffer wrapped, helper lied) must also fall back
    # rather than silently keeping everything and claiming it trimmed.
    missing = kmsg.Marker("run#not-in-log")
    missing.written = True
    text, trimmed = kmsg.run_log(lines, missing)
    check(trimmed is False,
          "a marker that never appears in the log is not reported as trimmed")
    check("WHOLE kernel ring buffer" in text,
          "and the header warns rather than implying a clean window")

    # dmesg-read returning nothing (a helper that rejects its own dmesg
    # options, a permission change) is a different failure from a marker that
    # was never written, and the header must say which -- the fixes do not
    # overlap. 20260831T1815 runs lost every by-change figure on both hosts
    # this way and the artifact blamed the marker.
    dead = kmsg.Marker("run#no-log-at-all")
    dead.written = True
    text, trimmed = kmsg.run_log([], dead)
    check(trimmed is False and "NO KERNEL LOG" in text,
          "an empty dmesg-read is called out as a read failure, not a marker one")
    check("WHOLE kernel ring buffer" not in text,
          "and is not misreported as the marker never being written")


def test_kmsg_capture():
    """The whole-run kernel-log capture (KmsgCapture)."""
    print("\nwhole-run kmsg capture")
    from lib import priv

    real_avail, real_argv = priv.available, priv.verb_argv
    tmp = tempfile.mkdtemp()
    try:
        priv.available = lambda: (True, "")

        # A stand-in for `dmesg --follow`: emit two lines, then block.
        dest = os.path.join(tmp, "kmsg.log")
        priv.verb_argv = lambda *_a: [
            "sh", "-c",
            "printf '%s\\n%s\\n' "
            "'<7>[1.0] snd-reloop-jockey3 1-1:1.0: Starting all URBs (cold start, grace 200 ms)' "
            "'<7>[1.1] snd-reloop-jockey3 1-1:1.0: Playback confirmed alive after 8 ms'; "
            "sleep 30"]
        cap = kmsg.KmsgCapture(dest)
        check(cap.start() is True, "capture starts when a helper is available")
        deadline = time.time() + 5
        while time.time() < deadline and not (
                os.path.exists(dest) and os.path.getsize(dest) > 0):
            time.sleep(0.05)
        path = cap.stop()
        check(path == dest, "stop() returns the path when the file has content")
        lines = kmsg.read_lines(dest)
        check(lines and any("confirmed alive after 8 ms" in ln for ln in lines),
              "the streamed lines land in the file", str(lines))
        check(cap._proc is None, "the follower process is reaped by stop()")

        # An empty capture is reported as nothing, not an empty path.
        dest2 = os.path.join(tmp, "empty.log")
        priv.verb_argv = lambda *_a: ["sh", "-c", "sleep 30"]
        cap2 = kmsg.KmsgCapture(dest2)
        cap2.start()
        time.sleep(0.2)
        check(cap2.stop() is None, "an empty capture returns None")
        check(kmsg.read_lines(dest2) is None, "and read_lines() agrees")

        # No helper, not root -> start() declines rather than raising.
        priv.available = lambda: (False, "no helper")
        check(kmsg.KmsgCapture(os.path.join(tmp, "x.log")).start() is False,
              "capture declines cleanly with no privileged route")
    finally:
        priv.available, priv.verb_argv = real_avail, real_argv


def test_restart_timing():
    """The URB-restart timing dataset: extraction, binning, percentiles."""
    print("\nrestart timing dataset")

    D = "snd-reloop-jockey3 1-1:1.0: "
    dmesg = "\n".join("[%s] %s%s" % (t, D, msg) for t, msg in [
        # A genuine cold restart: "Starting all URBs" arms, both directions
        # confirm against it.
        ("10.0", "PCM hw_params rate 48000, active_streams 1"),
        ("10.1", "Starting all URBs (cold start, grace 200 ms)"),
        ("10.2", "Waiting up to 200 ms for Playback to show liveness"),
        ("10.3", "Playback confirmed alive after 8 ms"),
        ("10.4", "Capture confirmed alive after 0 ms"),
        # A bare prepare() liveness check that waited out a stall: no
        # "Starting all URBs", and the PCM prepare disarmed the earlier one.
        ("20.0", "PCM prepare stream 0"),
        ("20.1", "Waiting up to 200 ms for Playback to show liveness"),
        ("20.2", "Playback confirmed alive after 112 ms"),
        # A warm restart. A "trigger cmd 1" between the two directions'
        # confirmations must NOT disarm.
        ("30.0", "PCM prepare stream 1"),
        ("30.1", "Starting all URBs (warm start, grace 150 ms)"),
        ("30.2", "Waiting up to 150 ms for Capture to stream steadily"),
        ("30.3", "Playback confirmed alive after 56 ms"),
        ("30.4", "PCM trigger stream 1, cmd 1"),
        ("30.5", "Capture confirmed streaming after 60 ms"),
    ])
    hist, grace = restart_timing.extract(dmesg)
    check(hist["playback|cold"] == {8: 1} and hist["capture|cold"] == {0: 1},
          "a confirmation after 'Starting all URBs' is a cold restart", str(hist))
    check(hist["playback|liveness"] == {112: 1},
          "a confirmation with no preceding restart is a liveness wait, not cold",
          str(hist))
    check(hist["playback|warm"] == {56: 1} and hist["capture|warm"] == {60: 1},
          "both directions bin as warm across an intervening 'trigger cmd 1'",
          str(hist))
    check(grace == {"cold": [200], "warm": [150]},
          "the grace ceilings in effect are recorded", str(grace))

    st = restart_timing.stats({8: 90, 12: 8, 64: 2}, percentiles=(50, 90, 99))
    check(st["n"] == 100 and st["p50"] == 8 and st["p99"] == 64,
          "percentiles are nearest-rank on the binned counts", str(st))

    st = restart_timing.stats({8: 990, 40: 8, 200: 2}, percentiles=(99, 99.9))
    check(st["p99"] == 8 and st["p99.9"] == 200,
          "a fractional percentile lands under its own 'p99.9' label", str(st))
    check(restart_timing.pct_label(90) == "p90"
          and restart_timing.pct_label(99.0) == "p99"
          and restart_timing.pct_label(99.9) == "p99.9",
          "pct_label drops a trailing .0 but keeps real decimals")

    check(restart_timing.tail_is_censored({8: 5, 190: 1}, [200]) is True,
          "a max that reaches the grace ceiling flags the tail as censored")
    check(restart_timing.tail_is_censored({8: 5, 40: 1}, [200]) is False,
          "a tail well short of the ceiling does not")

    # A debug kernel must not pollute the dataset.
    rj = {"run_id": "x/y", "results": [{"id": "JT-RATE-001"}],
          "env": {"detected_target": "x86_64-debug", "driver": {},
                  "kernel": {"arch": "x86_64", "release": "r",
                             "debug_options": ["KASAN", "DEBUG_KERNEL"]}}}
    rec, reason = restart_timing.source_from_run(rj, dmesg)
    check(rec is None and "debug kernel" in reason,
          "a KASAN kernel's timings are refused", reason)

    rj["env"]["kernel"]["debug_options"] = ["DEBUG_KERNEL"]
    rec, _ = restart_timing.source_from_run(rj, dmesg)
    check(rec and rec["dyndbg"] == "on" and "playback|cold" in rec["hist"],
          "a prod kernel with the lines present is ingested")

    # add_source is idempotent and replaces on change.
    data = {"version": 1, "sources": []}
    check(restart_timing.add_source(data, rec) is True, "first insert changes the dataset")
    check(restart_timing.add_source(data, dict(rec)) is False,
          "re-adding an identical record is a no-op")
    rec2 = dict(rec, driver_git="deadbee")
    check(restart_timing.add_source(data, rec2) is True and len(data["sources"]) == 1,
          "a changed record replaces rather than duplicates")

    agg = restart_timing.aggregate(data, dims=("arch", "stream", "start_type"))
    check(agg[("x86_64", "playback", "cold")] == {8: 1}
          and agg[("x86_64", "playback", "liveness")] == {112: 1},
          "aggregation groups per-source histograms by the requested dims", str(agg))


def test_pointer_rate():
    """The steady-state rate measurement, on synthetic traces.

    Timing a whole aplay/arecord invocation charges process start-up and device
    open to the sample rate and reads 10-17% low at four seconds. hw_ptr is the
    device's own frame counter, so its slope in steady state is the rate with
    all of that outside the window. These traces check that the window really
    does exclude what it claims to, because on hardware every one of these
    situations looks like a plausible number.
    """
    print("\nsteady-state rate from the hardware pointer")

    def trace(hz, seconds, startup=0.4, step=0.02, hold=None, reset_at=None):
        out, t, ptr = [], 0.0, 0
        while t < startup:                     # open, hw_params, prepare
            out.append((t, 0))
            t += step
        end = startup + seconds
        while t <= end:
            out.append((t, ptr))
            if reset_at is not None and abs(t - (startup + reset_at)) < step / 2:
                ptr = 0                        # a device reset restarts hw_ptr
            elif hold is None or not (hold[0] <= t - startup < hold[0] + hold[1]):
                ptr += int(hz * step)
            t += step
        return out

    r = alsa.pointer_rate(trace(48000, 4.0), 0.0)
    check(r.hz is not None and abs(r.hz / 48000 - 1.0) < 0.01,
          "a clean trace measures the rate to within 1%", repr(r))
    check(abs(r.first_motion_s - 0.4) < 0.05,
          "and reports the start-up it excluded", str(r.first_motion_s))

    # The whole point: the same trace timed end to end reads low, and by how
    # much is exactly the start-up over the duration.
    naive = 48000 * 4.0 / (4.0 + 0.4)
    check(abs(naive / 48000 - 1.0) > 0.08,
          "while timing the whole invocation would read 9% low",
          f"{naive:.0f} Hz")

    # A stall must not be averaged into the rate. Dragged through, a 1.5 s
    # plateau in a 4 s run reads ~37% slow -- which would report the fault this
    # driver has as the one it does not.
    r = alsa.pointer_rate(trace(48000, 4.0, hold=(1.0, 1.5)), 0.0)
    check(r.plateaus == 1 and abs(r.plateau_max_s - 1.5) < 0.05,
          "a plateau is reported as a stall, with its duration", repr(r))
    check(r.hz is not None and abs(r.hz / 48000 - 1.0) < 0.01,
          "and the rate is measured around it, not through it", repr(r))

    # The stream ending is not a stall. The watcher keeps sampling for a moment
    # after the process stops and before the substream closes, so every healthy
    # change ends with a static run -- counted, it would report a stall on all
    # of them and make the plateau count useless for finding real ones.
    tail = [(t, p) for t, p in trace(48000, 3.0)]
    tail += [(tail[-1][0] + 0.02 * i, tail[-1][1]) for i in range(1, 30)]
    r = alsa.pointer_rate(tail, 0.0)
    check(r.plateaus == 0 and r.tail_hold_s > 0.15,
          "a pointer still at the end of the trace is the stream stopping, "
          "not stalling", repr(r))
    check(r.hz is not None and abs(r.hz / 48000 - 1.0) < 0.01,
          "and it is still excluded from the measurement window", repr(r))

    # hw_ptr restarting at zero is a reset, not a stall, and must not produce a
    # negative or absurd rate.
    r = alsa.pointer_rate(trace(48000, 5.0, reset_at=2.0), 0.0)
    check(r.breaks == 1, "a backwards pointer is a break, not a plateau", repr(r))
    check(r.hz is not None and abs(r.hz / 48000 - 1.0) < 0.01,
          "and the rate is still measured, on one side of it", repr(r))

    # Settling is excluded, so a stream that starts slow and settles reads at
    # its settled rate rather than at the average of the two.
    slow_start = [(t, p) for t, p in trace(48000, 4.0)]
    r = alsa.pointer_rate(slow_start, 0.0, settle_s=0.5)
    check(r.window_start_s is not None and r.window_start_s >= 0.9,
          "the window starts after the settling time, not at the first frame",
          str(r.window_start_s))

    # Degenerate inputs must say why rather than divide by zero.
    check(alsa.pointer_rate([], 0.0).hz is None, "an empty trace has no rate")
    dead = alsa.pointer_rate([(t / 50, 0) for t in range(200)], 0.0)
    check(dead.hz is None and "never advanced" in dead.reason,
          "a pointer that never moves says so", repr(dead))
    short = alsa.pointer_rate(trace(48000, 0.6), 0.0)
    check(short.hz is None and "plateau-free window" in short.reason,
          "and so does a stream too short to measure", repr(short))


def test_param_overrides():
    """--param must yield typed values, not strings.

    The flag exists so that a parameter sweep does not require editing
    catalog.yaml between runs. That only works if `capture=false` turns off
    capture: left as the string "false" it is truthy, and the run would
    silently be the arm the operator was trying to compare against.
    """
    print("\nparameter overrides")
    sys.path.insert(0, HERE)
    import runner

    got = runner.parse_param_overrides(
        ["capture=false", "seconds_per_rate=4", "gap_seconds=2.5",
         "rates=[44100,96000]", "rate_change_stream=playback"])
    check(got["capture"] is False, "a JSON false is the boolean",
          repr(got["capture"]))
    check(got["seconds_per_rate"] == 4 and got["gap_seconds"] == 2.5,
          "numbers are numbers", repr(got))
    check(got["rates"] == [44100, 96000], "and a list is a list", repr(got))
    check(got["rate_change_stream"] == "playback",
          "while a bare word stays a string", repr(got["rate_change_stream"]))
    check(runner.parse_param_overrides(["k=a=b"])["k"] == "a=b",
          "only the first = separates key from value")
    check(runner.parse_param_overrides(["k=1", "k=2"])["k"] == 2,
          "the last occurrence of a key wins")
    try:
        runner.parse_param_overrides(["nokey"])
        check(False, "a value with no key is rejected")
    except SystemExit:
        check(True, "a value with no key is rejected")


def test_log_buf_len():
    """The printk ring buffer check has to read the same cmdline the kernel
    parsed, including the suffix forms memparse() accepts.

    Discovered 2026-08-20: alsa-test and pi4test both wrapped their default
    128 KiB buffer mid-run on a marker-heavy case (JT-RATE-003, 40000 markers
    over one run) and reported 94% of markers "missing" -- indistinguishable,
    from the run's own output, from the marker-charset-rejection failure mode
    test_marker_labels() covers above. runner.preflight() now warns below
    LOG_BUF_LEN_MIN so this gets caught on any machine before a run wastes
    hours on suppressed diagnostics, not just the two it was found on.
    """
    print("\nprintk ring buffer size")
    real_read = env._read
    real_kernel_config = env.kernel_config
    try:
        env._read = lambda path, default="": (
            "console=tty0 log_buf_len=4M quiet" if path == "/proc/cmdline"
            else real_read(path, default))
        check(env.log_buf_len() == 4 * 1024 * 1024,
              "an M suffix on the boot param is read as mebibytes",
              env.log_buf_len())

        env._read = lambda path, default="": (
            "log_buf_len=1048576" if path == "/proc/cmdline"
            else real_read(path, default))
        check(env.log_buf_len() == 1048576,
              "a bare byte count needs no suffix", env.log_buf_len())

        env._read = lambda path, default="": (
            "log_buf_len=256K" if path == "/proc/cmdline"
            else real_read(path, default))
        check(env.log_buf_len() == 256 * 1024,
              "a K suffix is read as kibibytes", env.log_buf_len())

        env._read = lambda path, default="": (
            "quiet console=tty0" if path == "/proc/cmdline"
            else real_read(path, default))
        env.kernel_config = lambda: {"LOG_BUF_SHIFT": "17"}
        check(env.log_buf_len() == 1 << 17,
              "absent the boot param, CONFIG_LOG_BUF_SHIFT is the fallback",
              env.log_buf_len())

        env.kernel_config = lambda: {}
        check(env.log_buf_len() is None,
              "and with neither available it says so rather than guessing")
    finally:
        env._read = real_read
        env.kernel_config = real_kernel_config

    sys.path.insert(0, HERE)
    import runner
    check(runner.LOG_BUF_LEN_MIN == 4 * 1024 * 1024,
          "runner.preflight()'s threshold matches what tests/README.md "
          "documents (log_buf_len=4M)", runner.LOG_BUF_LEN_MIN)


def main():
    rules = load(os.path.join("lib", "rules.yaml"))
    catalog = load("catalog.yaml")
    targets = load("targets.yaml")
    profiles = load("profiles.yaml")

    test_classifier(rules)
    test_wedged_device(rules)
    test_watchdog(rules)
    test_recovery_giveup(rules)
    test_error_handling(rules)
    test_kunit_on_target(rules)
    test_targets(targets)
    test_build_id()
    test_catalog(catalog, targets, profiles)
    test_capabilities(targets)
    test_capability_gating()
    test_results_roundtrip()
    test_terminal()
    test_operator_prompts()
    test_semi_automated_routing()
    test_case_streaming()
    test_manual_fallback()
    test_run_resolution()
    test_privilege_boundary()
    test_rate_change_case_runs()
    test_rate_change_log_attribution()
    test_marker_labels()
    test_run_log_trimming()
    test_kmsg_capture()
    test_restart_timing()
    test_log_buf_len()
    test_pointer_rate()
    test_param_overrides()

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    if _failures:
        print("\nfailed:")
        for f in _failures:
            print(f"  {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
