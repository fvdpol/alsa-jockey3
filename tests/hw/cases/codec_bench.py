#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L2: user-space codec correctness across all variants.

Wraps tests/codec/codecbench.py, which compares the reference, 64-bit and
32-bit codecs plus any candidates against the golden vectors. It also verifies
that its working copy of the driver source is still in sync, so a result
cannot be attributed to the wrong revision.
"""

import os
import re
import sys

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402

LINE_RE = re.compile(r"^\s+(\S+)\s+\[(driver|candidate)\]\s+(PASS|FAIL)", re.M)


def main():
    c = Case()
    bench = os.path.join(c.repo, "tests", "codec", "codecbench.py")
    if not os.path.exists(bench):
        c.blocked(f"{bench} not found")

    sub = sys.argv[1] if len(sys.argv) > 1 else "test"
    rc, out, err = c.run([sys.executable, bench, sub], timeout=1800)

    with open(os.path.join(c.workdir, "bench.log"), "w", encoding="utf-8") as f:
        f.write(out or "")
        f.write(err or "")

    hits = LINE_RE.findall(out or "")
    c.metric("variants_tested", len(hits))
    for name, _kind, verdict in hits:
        if verdict != "PASS":
            c.fail(f"codec variant {name} failed")

    if rc != 0 and not c.failed:
        c.fail(f"codecbench {sub} exited {rc}")
    if not hits and not c.failed:
        c.fail("no codec variants were tested")

    c.done()


if __name__ == "__main__":
    main()
