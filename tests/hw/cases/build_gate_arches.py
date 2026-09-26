#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""JT-BUILD-005: cross-arch W=1/sparse warnings gate.

Thin wrapper around tests/build/build_jockey3_arches.sh, the same shape as
build_gate.py's wrap of build_jockey3.sh: one script owns the build logic,
this only runs it, reads its JSON report and turns that into case metrics and
a pass/fail.
"""

import json
import os
import sys

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402


def main():
    c = Case()

    script = os.path.join(c.repo, "tests", "build", "build_jockey3_arches.sh")
    if not os.path.exists(script):
        c.blocked(f"{script} not found")

    build_tree = os.environ.get("BUILD_TREE") or os.path.expanduser("~/sound-build")
    if not os.path.isdir(os.path.join(build_tree, "scripts")):
        # Same reasoning as build_gate.py: a hardware rig with no build tree
        # is not this gate's job, and a hard "blocked" would put a phantom
        # coverage gap in every hardware run.
        c.skip(f"no build tree at {build_tree}: this is not a build host. "
               f"Run this on the build server, or set BUILD_TREE.")

    report = os.path.join(c.workdir, "arches.json")
    env = dict(os.environ)
    env["BUILD_TREE"] = build_tree
    env["JSON_REPORT"] = report

    rc, out, err = c.run(["bash", script], timeout=3600, env=env)

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
                if not g.get("passed", True):
                    c.fail(f"{g.get('name')}: {g.get('detail', 'failed')}")
        except (OSError, ValueError) as e:
            c.note(f"report unreadable: {e}")
    else:
        c.note("no JSON report produced")

    if rc != 0 and not c.failed:
        c.fail(f"arches gate failed (exit {rc})")
    c.done()


if __name__ == "__main__":
    main()
