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
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: apt install python3-yaml")

from lib import env, kmsg, results          # noqa: E402

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
        return yaml.safe_load(f)


# --------------------------------------------------------------- classifier

def test_classifier(rules):
    print("\nkernel message classification")
    c = kmsg.Classifier(rules)

    lines = [
        DRIVER + "Capture URB has stalled.",
        DRIVER + "jockey3_pcm_prepare waited 336 ms for reset completion.",
        DRIVER + "jockey3_pcm_prepare waited 1012 ms for reset completion.",
        DRIVER + "Inconsistent URB in-flight count: playback=2 != 0",
        DRIVER + "Some message nobody has ever seen",
        "usb 1-3: new high-speed USB device number 7 using xhci_hcd",
        "wlan0: authenticate with aa:bb:cc:dd:ee:ff",
        "BUG: unable to handle kernel NULL pointer dereference at 0000",
        DRIVER + "Playback URB has stalled.",
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


def test_kunit_on_target(rules):
    print("\ncodec KUnit output at module load")
    c = kmsg.Classifier(rules)
    b, m = c.classify([
        "    KTAP version 1",
        "    # Subtest: ploytec-codec",
        "    ok 1 ploytec_test_encode_known_vectors",
        "    1..75",
        "    # ploytec-codec: pass:75 fail:0 skip:0 total:75",
        "    ok 1 ploytec-codec",
    ], [])
    check(not b[kmsg.UNEXPECTED] and not b[kmsg.UNCLASSIFIED],
          "a passing suite does not pollute the run", str(kmsg.summarize(b)))
    check(m.get("kunit_passed_on_target") == [75],
          "the pass count is recorded as a metric", str(m))

    b2, _ = c.classify(["    not ok 3 ploytec_test_encode_is_linear"], [])
    check(len(b2[kmsg.UNEXPECTED]) == 1, "a failing codec case fails the run")


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

    bad = [c["id"] for c in catalog["cases"]
           if c["status"] == "implemented" and c["mode"] == "automated"
           and not c.get("exec")]
    check(not bad, "implemented automated cases name an executable", str(bad))

    bad = [c["id"] for c in catalog["cases"]
           if c.get("exec") and not os.path.exists(os.path.join(HERE, c["exec"]))]
    check(not bad, "every named executable exists", str(bad))

    bad = [c["id"] for c in catalog["cases"]
           if c["status"] == "implemented" and c["mode"] == "manual"
           and not c.get("steps")]
    check(not bad, "implemented manual cases have steps to follow", str(bad))


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


def main():
    rules = load(os.path.join("lib", "rules.yaml"))
    catalog = load("catalog.yaml")
    targets = load("targets.yaml")
    profiles = load("profiles.yaml")

    test_classifier(rules)
    test_kunit_on_target(rules)
    test_targets(targets)
    test_build_id()
    test_catalog(catalog, targets, profiles)
    test_results_roundtrip()

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    if _failures:
        print("\nfailed:")
        for f in _failures:
            print(f"  {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
