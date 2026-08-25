#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L3: load and unload the driver, and check what it created.

Also the case that captures the firmware revision for the run, because that is
only emitted at probe. It should therefore run early in every profile -- which
is why every profile in profiles.yaml starts with it.

The shape check matters as much as the load succeeding: a module that loads
and registers nothing is a pass by any naive criterion.
"""

import os
import sys
import time

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402
from lib import alsa, env, priv    # noqa: E402


def wait_for_card(present, timeout=10.0):
    """USB enumeration is not instantaneous; neither is teardown."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        idx, _ = alsa.find_card()
        if (idx is not None) == present:
            return idx, time.time()
        time.sleep(0.1)
    idx, _ = alsa.find_card()
    return idx, None


def main():
    c = Case()
    ok, why = priv.available()
    if not ok:
        c.blocked(f"cannot load or unload the module: {why}")

    iterations = int(c.params.get("iterations_per_run", 3))
    settle_delay_s = float(c.params.get("settle_delay_s", 0))

    # The firmware message is a dev_dbg, and the only place the firmware
    # revision appears -- there is no sysfs attribute, and adding one would be
    # a change to the driver made for tests. It is emitted at probe and again
    # on every rate change, because ploytec_get_firmware() sits inside
    # ploytec_initialize_device(), which jockey3_set_rate() also calls.
    #
    # Nothing to enable here: priv.load_module() passes the rule as modprobe's
    # dyndbg= parameter on every load, which is the only way to have it in
    # place before probe runs. Enabling it once per case does not work, because
    # dynamic_debug drops a module's rules each time it unloads.

    load_ms, unload_ms = [], []

    for i in range(1, iterations + 1):
        if env.module_loaded():
            priv.unload_module(timeout=30)
            wait_for_card(False)

        t0 = time.time()
        rc, _out, err = priv.load_module()
        if rc != 0:
            c.fail(f"iteration {i}: load failed: {err.strip()[:120]}")
            break

        idx, seen_at = wait_for_card(True)
        if idx is None:
            c.fail(f"iteration {i}: module loaded but no card appeared")
            break
        load_ms.append(round((seen_at - t0) * 1000, 1))

        subs = alsa.substreams(idx)
        if not subs["playback"]:
            c.fail(f"iteration {i}: no playback substream")
        if not subs["capture"]:
            c.fail(f"iteration {i}: no capture substream")
        if not subs["rawmidi"]:
            c.fail(f"iteration {i}: no rawmidi device")

        if settle_delay_s:
            time.sleep(settle_delay_s)

        t0 = time.time()
        rc, _out, err = priv.unload_module()
        if rc != 0:
            c.fail(f"iteration {i}: unload failed: {err.strip()[:120]}")
            break
        _, gone_at = wait_for_card(False)
        if gone_at is None:
            c.fail(f"iteration {i}: card still present after rmmod")
            break
        unload_ms.append(round((gone_at - t0) * 1000, 1))

        c.progress(f"load {i}/{iterations}  up {load_ms[-1]:>6.0f} ms  "
                   f"down {unload_ms[-1]:>6.0f} ms  card hw:{idx}")

    # Leave the driver loaded: every case after this one expects a card.
    if not env.module_loaded():
        priv.load_module()
        wait_for_card(True)

    if load_ms:
        c.metric("load_ms", {"n": len(load_ms), "min": min(load_ms),
                             "max": max(load_ms),
                             "mean": round(sum(load_ms) / len(load_ms), 1)})
    if unload_ms:
        c.metric("unload_ms", {"n": len(unload_ms), "min": min(unload_ms),
                               "max": max(unload_ms),
                               "mean": round(sum(unload_ms) / len(unload_ms), 1)})

    d = env.driver_info()
    c.metric("build_id", d.get("build_id"))
    if d.get("build"):
        c.metric("git_describe", d["build"].get("git_describe"))
    else:
        c.note("no build manifest for this build-id -- the git revision "
               "under test is unknown")

    c.done()


if __name__ == "__main__":
    main()
