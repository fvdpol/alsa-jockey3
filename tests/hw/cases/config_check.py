#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""JT-BUILD-006: the running kernel's config matches what its target claims.

Every other case in a run is only as trustworthy as the kernel it ran under.
A -prod kernel quietly built with KASAN still enumerates, plays and captures
-- nothing else in the suite would ever notice -- and would silently pollute
that target's timing numbers exactly as targets.yaml's own preflight
`verify_target()` check warns about, but that check only looks at three
symbols. This reads the RUNNING kernel's actual config out of
/proc/config.gz and checks it against every symbol tests/configs/derive-prod.sh
and check-debug-config.sh already care about, by parsing
tests/configs/config-flags.sh directly rather than copying its symbol lists
here -- a second hand-maintained copy is exactly the kind of drift that file
exists to prevent.
"""

import os
import re
import sys

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402
from lib import env                # noqa: E402

REQUIRED_LISTS = ("DEBUG_ONLY", "DEBUG_REQUIRED", "ALWAYS_ON", "ALWAYS_OFF")
# Present in config-flags.sh but not on every check that used to exist before
# it was added, so it is parsed but not required.
OPTIONAL_LISTS = ("DEBUG_REQUIRED_EXEMPT",)


def parse_flag_lists(path):
    """Extract the bash arrays from config-flags.sh.

    Regex, not a bash subprocess: the arrays are plain whitespace-separated
    symbol names with '#'-prefixed comment lines, and shelling out just to
    read a literal would be a stranger dependency than parsing it.
    """
    text = open(path, encoding="utf-8").read()
    lists = {}
    for name in REQUIRED_LISTS + OPTIONAL_LISTS:
        m = re.search(rf"^{name}=\((.*?)^\)", text, re.S | re.M)
        if not m:
            continue
        syms = []
        for line in m.group(1).splitlines():
            syms.extend(line.split("#", 1)[0].split())
        lists[name] = syms
    return lists


def main():
    c = Case()

    flags_path = os.path.join(c.repo, "tests", "configs", "config-flags.sh")
    if not os.path.exists(flags_path):
        c.blocked(f"{flags_path} not found")
    lists = parse_flag_lists(flags_path)
    missing = [n for n in REQUIRED_LISTS if n not in lists]
    if missing:
        c.blocked(f"could not parse {', '.join(missing)} from {flags_path}")

    kernel = env.kernel_info()
    if not kernel.get("config_available"):
        c.fail("running kernel's config is not readable -- need "
                "/proc/config.gz or /boot/config-*, i.e. CONFIG_IKCONFIG_PROC")
        c.done()

    lv = kernel.get("localversion") or kernel.get("release") or ""
    if "-alsa-debug" in lv:
        flavor = "debug"
    elif "-alsa-prod" in lv:
        flavor = "prod"
    else:
        c.fail(f"running kernel's LOCALVERSION ('{lv}') names neither "
               f"-alsa-debug nor -alsa-prod, so it cannot be checked "
               f"against either config-flags.sh profile")
        c.done()

    cfg = env.kernel_config()

    def is_on(sym):
        return cfg.get(sym) in ("y", "m")

    for sym in lists["ALWAYS_ON"]:
        if not is_on(sym):
            c.fail(f"CONFIG_{sym} should be on in every target -- "
                   f"the test framework depends on it -- but is not")
    for sym in lists["ALWAYS_OFF"]:
        if is_on(sym):
            c.fail(f"CONFIG_{sym} must never be on, but is")

    if flavor == "debug":
        arch = kernel.get("arch")
        exempt = {
            pair.split(":", 1)[1]
            for pair in lists.get("DEBUG_REQUIRED_EXEMPT", [])
            if pair.split(":", 1)[0] == arch
        }
        for sym in lists["DEBUG_REQUIRED"]:
            if sym in exempt:
                continue
            if not is_on(sym):
                c.fail(f"CONFIG_{sym} should be on for a debug kernel "
                       f"but is not")
    else:
        for sym in lists["DEBUG_ONLY"]:
            if is_on(sym):
                c.fail(f"CONFIG_{sym} should be off for a production "
                       f"kernel but is on")

    c.note(f"flavor detected from LOCALVERSION: {flavor}")
    c.done()


if __name__ == "__main__":
    main()
