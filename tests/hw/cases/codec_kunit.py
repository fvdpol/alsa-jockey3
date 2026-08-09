#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L2: run the in-kernel codec KUnit suite.

Thin wrapper around tests/codec/run_kunit.sh, which already knows the cross
compilers, the QEMU binaries and the two per-architecture config quirks. The
value added here is recording the pass count as a metric, so a suite that
silently starts running fewer tests is visible.
"""

import os
import re
import sys

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402

RESULT_RE = re.compile(r"Ran (\d+) tests: passed: (\d+)(?:, failed: (\d+))?")


def main():
    c = Case()
    script = os.path.join(c.repo, "tests", "codec", "run_kunit.sh")
    if not os.path.exists(script):
        c.blocked(f"{script} not found")

    kernel_src = os.environ.get("KERNEL_SRC") or os.path.expanduser("~/sound")
    if not os.path.isdir(os.path.join(kernel_src, "tools", "testing", "kunit")):
        c.blocked(f"no kernel tree with kunit.py at {kernel_src} "
                  f"(set KERNEL_SRC)")

    targets = sys.argv[1:] or ["um"]
    rc, out, err = c.run(["bash", script] + targets, timeout=3600)

    with open(os.path.join(c.workdir, "kunit.log"), "w", encoding="utf-8") as f:
        f.write(out or "")
        f.write(err or "")

    total = passed = failed = 0
    for m in RESULT_RE.finditer(out or ""):
        total += int(m.group(1))
        passed += int(m.group(2))
        failed += int(m.group(3) or 0)

    c.metric("tests_run", total)
    c.metric("tests_passed", passed)
    c.metric("arches_run", len(targets))
    if failed:
        c.metric("tests_failed", failed)

    if rc != 0:
        c.fail(f"run_kunit.sh {' '.join(targets)} exited {rc}")
    elif total == 0:
        # Exit 0 with no parsed results means the output format changed, which
        # would otherwise be reported as a silent pass -- the worst outcome a
        # test suite can have.
        c.fail("no KUnit results parsed from output")
    elif passed != total:
        c.fail(f"{total - passed} of {total} tests failed")

    c.done()


if __name__ == "__main__":
    main()
