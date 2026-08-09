#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L1 static and build gates.

Wraps tests/build/build_jockey3.sh, which runs every gate and emits a JSON
report. Each gate is a separate case with its own ID and its own trend line --
one overall pass/fail would hide which gate moved, and "the build broke" is a
much less useful thing to learn than "checkpatch gained two warnings".

    build_gate.py checkpatch | build | docs | size
"""

import json
import os
import sys

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402

GATE_METRICS = {
    "checkpatch": ["checkpatch_warnings", "checkpatch_errors"],
    "build": ["build_warnings"],
    "docs": [],
    "size": ["ko_bytes", "text_bytes", "data_bytes", "bss_bytes"],
}


def main():
    c = Case()
    gate = sys.argv[1] if len(sys.argv) > 1 else "checkpatch"
    if gate not in GATE_METRICS:
        c.blocked(f"unknown gate '{gate}'")

    script = os.path.join(c.repo, "tests", "build", "build_jockey3.sh")
    if not os.path.exists(script):
        c.blocked(f"{script} not found")

    kernel_src = os.environ.get("KERNEL_SRC") or os.path.expanduser("~/sound")
    if not os.path.isdir(os.path.join(kernel_src, "scripts")):
        c.blocked(f"no kernel tree at {kernel_src} (set KERNEL_SRC)")

    report = os.path.join(c.workdir, "gates.json")
    env = dict(os.environ)
    env["KERNEL_SRC"] = kernel_src
    env["JSON_REPORT"] = report

    rc, out, err = c.run(["bash", script, "--gate", gate],
                         timeout=3600, env=env)

    with open(os.path.join(c.workdir, "build.log"), "w", encoding="utf-8") as f:
        f.write(out or "")
        f.write(err or "")

    if os.path.exists(report):
        try:
            with open(report, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in (data.get("metrics") or {}).items():
                c.metric(k, v)
            for g in data.get("gates") or []:
                if g.get("name") == gate and not g.get("passed", True):
                    c.fail(f"{gate}: {g.get('detail', 'failed')}")
        except (OSError, ValueError) as e:
            c.note(f"report unreadable: {e}")
    else:
        c.note("no JSON report produced")

    # JT-BUILD-004 is metric-only: it records size and never fails. A
    # threshold would either be too loose to catch anything real or would
    # block a legitimate feature.
    if gate == "size":
        c.done()

    if rc != 0 and not c.failed:
        c.fail(f"{gate} gate failed (exit {rc})")
    c.done()


if __name__ == "__main__":
    main()
