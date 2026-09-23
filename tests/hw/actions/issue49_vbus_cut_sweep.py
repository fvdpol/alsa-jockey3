#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""issue49 -- does a VBUS-only cut (device stays powered) reproduce the
ploytec_initialize_device() -EPROTO failures JT-RATE-004 sees on i386-prod,
independently of the rapid mid-transaction reopening JT-RATE-004 also does?

NEVER MERGE, NEVER RUN except on the test/issue49-mid-transaction-power-loss
branch. See github.com/fvdpol/alsa-jockey3/issues/49 (xref #48).

THE CONFOUND THIS SCRIPT EXISTS TO SEPARATE OUT
------------------------------------------------
issue48_settle_sweep.py's power.cycle() and JT-RATE-004's power cut are NOT
the same operation, despite both being called "power cycling" loosely in
earlier discussion of this issue. lib.power.cycle() switches the device's
own mains supply -- a real cold boot, capacitors drained, firmware restarts
from scratch. Its own docstring is explicit that this is the point: "the
point of this whole exercise is a cold boot ... reconnecting mains before
the device's own supply has drained gives a warm one, which is the case
that already works and proves nothing."

priv.usb_power() switches the *hub port*, i.e. VBUS only. The Jockey 3 is
self-powered (see lib/power/__init__.py's own header), so the device's
firmware never reboots across this cut -- from the device's side this is
the "warm reconnect" case lib.power's docstring already flags as easy and
uninformative for the cold-boot race #48 characterized. JT-RATE-004 uses
priv.usb_power() exclusively. The 0/100 clean result posted to #48 was
lib.power.cycle() (cold boot) at the shipped usleep_range(25, 35) -- it does
not, on its own, say anything about the warm-reconnect case.

Three arms, same fixed settle delay (this script does not reload/sweep the
module -- reload once at the value under test before running either arm):

    idle       -- priv.usb_power("off"), wait, priv.usb_power("on").
                  Nothing else happening. Isolates "does a VBUS-only warm
                  reconnect alone elevate the failure rate", independent
                  of any concurrent USB activity. 0/30 clean.
    active     -- mirrors JT-RATE-004's own race_rate_change_against_cut():
                  fire usb_power("off") in the background (the privileged
                  helper takes 2-4s round-tripping through uhubctl) and,
                  while that resolves, keep reopening aplay at a rotating
                  rate every 20-120ms until the card actually disappears.
                  11/30-12/20 flapped, depending on run.
    streaming  -- splits what "active" conflates: one aplay opened ONCE
                  (no reopening, no rate rotation) and held across the
                  whole cut/reconnect window. Isolates whether rapid
                  *reopening* specifically matters, or whether merely
                  having a stream open and flowing at cut time is already
                  enough on its own. 0/30 clean, trace-confirmed (no error
                  return codes anywhere in 30 saved traces) -- rapid
                  reopening is the necessary ingredient, not just being
                  open/streaming at cut time.

Held fixed across all arms and matched to JT-RATE-004's own defaults:
off_seconds, settle timeout, race jitter window, rates. Only the
precondition before the cut varies.

    issue49_vbus_cut_sweep.py --arm idle|active|streaming [--cycles 30]
        [--off-seconds 2] [--timeout 15] [--race-ms-min 20]
        [--race-ms-max 120] [--rates 44100,48000,96000] [--out results.json]
        [--save-trace DIR]

--save-trace reuses #48's own trace_printk() instrumentation
(issue48_trace_enabled=1). ISSUE48_TRACE() now covers the whole
post-reconnect sequence: ploytec_initialize_device() (get_firmware,
set_interface x4, the settle delay, clear_halt x3, get_status),
ploytec_get_rate(), ploytec_set_rate() (each burst write individually), and
ploytec_start_streaming(). Confirmed: the most common failure shape
(set_rate_ep) traces to the same specific point every time --
"set_rate burst(0) done ret=-71", the first SET_RATE write right after
get_rate succeeds, with ploytec_initialize_device() clean throughout.

The module must already be loaded with issue48_trace_enabled=1 (this script
does not reload it -- see issue48_settle_sweep.py's reload_with() for how).

Every run starts with a real mains power cycle by default (lib.power.cycle(),
not priv.usb_power() -- see below for why that distinction matters), so the
device's firmware starts from the same known-clean state each time,
independent of whatever state the previous run left it in. --no-power-cycle-first
skips this, for deliberately continuing to poke at a state a prior run left
behind (e.g. following up on #50).
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib import alsa, kmsg, power, priv   # noqa: E402

FAIL_MARKER = "probe with driver snd-reloop-jockey3 failed with error"
OK_MARKER = "Reloop Jockey 3 Remix Firmware"
CHANNELS = 4
FORMAT = "S24_3LE"
TRACE_PATH = "/sys/kernel/debug/tracing/trace"

# Classifies the *first, chronologically earliest* failure in a cycle's
# window. Two levels, found the hard way by comparing raw dmesg across two
# consecutive cycles (issue #49): a driver-level one, where USB enumeration
# itself succeeded and one of ploytec_initialize_device()'s/ploytec_set_rate()'s
# own EP0 transfers timed out (visible to ISSUE48_TRACE, inside the driver);
# and a USB-core-level one, "device descriptor read/64, error", which fires
# *before* probe() is ever entered -- the most basic step of enumeration
# failing, invisible to ISSUE48_TRACE entirely since the driver never runs.
# Order here is NOT priority -- classify_failure_shape() scans lines in
# their actual order and returns whichever pattern is hit first, since which
# level fails first is exactly the question, not something to assume.
FAILURE_SHAPE_PATTERNS = [
    ("usb_core_descriptor_read", re.compile(r"device descriptor read/\d+, error")),
    ("firmware_read", re.compile(r"Firmware version read failed")),
    ("clear_halt", re.compile(r"Failed to clear halt on EP")),
    ("set_rate_ep", re.compile(r"Failed to set rate on EP")),
    # start_streaming is its own step (ploytec_start_streaming(), called right
    # after ploytec_initialize_device() succeeds, from both
    # jockey3_initialize_ploytec()'s probe-time retry loop and
    # jockey3_set_rate()) -- split out from the old catch-all bucket after
    # finding "Ploytec failed to start streaming" as a cycle's first failure,
    # with ploytec_initialize_device() having silently succeeded just before.
    ("start_streaming", re.compile(r"(?:Ploytec failed to start streaming|"
                                    r"Failed to start streaming after rate change)")),
    ("get_status_or_get_rate", re.compile(r"Failed to (?:initialize device to change rate|"
                                           r"read current hardware rate)")),
]


def classify_failure_shape(seen_lines):
    """Returns (shape, usb_core_retries) -- shape is the first-hit pattern
    label in chronological order (None if the cycle had no failure at all),
    usb_core_retries is a count of the usb_core_descriptor_read pattern
    specifically, since that one can recur several times (several device
    numbers in a row) without ever producing a "probe ... failed" line, so
    it is invisible to the `attempts` metric entirely."""
    shape = None
    usb_core_retries = 0
    for line in seen_lines:
        for label, pattern in FAILURE_SHAPE_PATTERNS:
            if pattern.search(line):
                if label == "usb_core_descriptor_read":
                    usb_core_retries += 1
                if shape is None:
                    shape = label
                break
    if shape is None and any(FAIL_MARKER in l for l in seen_lines):
        shape = "unclassified"
    return shape, usb_core_retries


def clear_trace():
    """Reset ftrace's ring buffer so each cycle's saved trace is just its
    own -- same reasoning as issue48_settle_sweep.py's clear_trace(), but
    using tee for tracing_on too instead of `sh -c "echo 1 > ..."`: the
    sh -c form didn't match the sudoers NOPASSWD rule it was given (fell
    back to a password prompt, failed silently under check=False) -- every
    trace saved with that form came back with entries-in-buffer/written:
    0/0. tee is the same pattern already proven to work for TRACE_PATH
    itself."""
    subprocess.run(["sudo", "tee", TRACE_PATH], input="", text=True,
                    capture_output=True, check=False)
    r = subprocess.run(["sudo", "tee", f"{TRACE_PATH.rsplit('/', 1)[0]}/tracing_on"],
                        input="1", text=True, capture_output=True, check=False)
    if r.returncode != 0:
        print(f"warning: could not enable tracing_on: {r.stderr.strip()}",
              file=sys.stderr)


def save_trace(dest):
    r = subprocess.run(["sudo", "cat", TRACE_PATH], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    with open(dest, "w", encoding="utf-8") as f:
        f.write(r.stdout)
    return r.stdout


def start_playback(device, rate, seconds=2.0):
    """A short playback stream at @rate -- same shape as JT-RATE-004's own
    start_playback(), sox feeding aplay, so a fresh rate is actually asked
    for and not just replayed from an already-open stream."""
    gen = subprocess.Popen(
        ["sox", "-n", "-r", str(rate), "-c", str(CHANNELS), "-b", "24",
         "-e", "signed-integer", "-t", "raw", "-",
         "synth", str(seconds), "sine", "A3", "gain", "-12"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    p = subprocess.Popen(
        ["aplay", "-D", device, "-r", str(rate), "-c", str(CHANNELS),
         "--format", FORMAT, "-t", "raw"],
        stdin=gen.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    gen.stdout.close()
    return gen, p


def reap(gen, p):
    for proc in (p, gen):
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def wait_for_card(present, timeout):
    """Same shape as JT-RATE-004's own wait_for_card()."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        idx, _ = alsa.find_card()
        if idx is not None and present:
            if alsa.control_is_live(idx):
                return idx
        elif idx is None and not present:
            return None
        time.sleep(0.05)
    idx, _ = alsa.find_card()
    return idx


def cut_power_async():
    box = {}

    def run():
        box["rc"], box["out"], box["err"] = priv.usb_power("off")

    import threading
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def race_reopen_until_gone(idx, rates, seed, race_lo, race_hi, deadline_s=30):
    """JT-RATE-004's race_rate_change_against_cut(), trimmed to what this
    script needs: keep reopening aplay at a rotating rate until the card is
    gone. Returns the number of reopen attempts (not itself a metric this
    script reports -- kept only because interrupting mid-open cleanly needs
    the same reap() dance JT-RATE-004 uses)."""
    device = alsa.device_name(idx)
    gen = p = None
    attempt = 0
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        now_idx, _ = alsa.find_card()
        if now_idx is None:
            break
        if p is not None:
            reap(gen, p)
        attempt += 1
        rate = rates[(seed + attempt) % len(rates)]
        gen, p = start_playback(device, rate)
        time.sleep(random.uniform(race_lo, race_hi))
    if p is not None:
        reap(gen, p)
    return attempt


def hold_stream_until_gone(idx, rate, deadline_s=30):
    """Open ONE aplay at @rate and hold it -- no reopening, no rate
    rotation -- until the card disappears or @deadline_s elapses.

    Isolates "streaming" from "active": race_reopen_until_gone() conflates
    two things that might independently matter -- rapid *reopening*
    (a fresh hw_params()/rate change every 20-120ms) and simply having a
    stream open and flowing at the moment VBUS drops. This holds only the
    second condition, with nothing reopening at all."""
    device = alsa.device_name(idx)
    gen, p = start_playback(device, rate, seconds=deadline_s + 5)
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        now_idx, _ = alsa.find_card()
        if now_idx is None:
            break
        if p.poll() is not None:
            break
        time.sleep(0.2)
    reap(gen, p)


def wait_for_settle(marker, timeout):
    """Same shape as issue48_settle_sweep.py's wait_for_settle(): one sleep,
    then read the log once -- no polling during the window (see that
    script's docstring for why polling itself perturbs this measurement).
    Also classifies the first failure's shape, per FAILURE_SHAPE_PATTERNS --
    unconditionally, not gated on `fails`: a USB-core-level enumeration
    failure ("device descriptor read/64, error") can happen without ever
    producing a "probe ... failed" line at all, since probe() is never
    reached for that device number -- gating on `fails` would silently miss
    it entirely.

    `settled` from the log alone is necessary but not sufficient: the
    firmware-print string proves probe() got that far at some point in the
    window, not that the card is still alive by the time this returns --
    a wedge arriving after a clean probe (see #50) would still show
    OK_MARKER in the log. Callers must confirm actual end-of-window
    liveness themselves (see run_cycle()) before trusting this "settled"."""
    time.sleep(timeout)
    log = kmsg.read_log()
    idx = None
    for i, line in enumerate(log):
        if marker.token in line:
            idx = i
    seen = log[idx + 1:] if idx is not None else log
    fails = sum(1 for l in seen if FAIL_MARKER in l)
    settled = any(OK_MARKER in l for l in seen)
    shape, usb_core_retries = classify_failure_shape(seen)
    return (fails + 1 if settled else fails), settled, shape, usb_core_retries


def run_cycle(i, arm, rates, off_seconds, timeout, race_lo, race_hi, trace_dir):
    idx, _ = alsa.find_card()
    if idx is None:
        return {"attempts": None, "settled": False, "error": "no card at cycle start"}

    if trace_dir:
        clear_trace()

    marker = kmsg.Marker(f"issue49-{arm}-{i}")

    if arm == "idle":
        marker.write()
        rc, _out, err = priv.usb_power("off")
        if rc != 0:
            return {"attempts": None, "settled": False,
                    "error": f"usb-power off failed: {(err or '').strip()[:120]}"}
    elif arm == "streaming":
        off_thread, off_box = cut_power_async()
        marker.write()
        hold_stream_until_gone(idx, rates[i % len(rates)])
        off_thread.join(timeout=30)
        if off_box.get("rc") != 0:
            return {"attempts": None, "settled": False,
                    "error": f"usb-power off failed: "
                             f"{(off_box.get('err') or '').strip()[:120]}"}
    else:
        off_thread, off_box = cut_power_async()
        marker.write()
        race_reopen_until_gone(idx, rates, i, race_lo, race_hi)
        off_thread.join(timeout=30)
        if off_box.get("rc") != 0:
            return {"attempts": None, "settled": False,
                    "error": f"usb-power off failed: "
                             f"{(off_box.get('err') or '').strip()[:120]}"}

    gone_idx = wait_for_card(False, timeout)
    if gone_idx is not None:
        return {"attempts": None, "settled": False, "error": "card never left"}

    time.sleep(off_seconds)

    rc, _out, err = priv.usb_power("on")
    if rc != 0:
        return {"attempts": None, "settled": False,
                "error": f"usb-power on failed: {(err or '').strip()[:120]}"}

    attempts, settled, shape, usb_core_retries = wait_for_settle(marker, timeout)

    # wait_for_settle() only proves probe() got there at some point in the
    # window -- confirm the card is still actually alive now, since a wedge
    # arriving after a clean probe (#50) would otherwise still read as
    # "settled". Card gone here despite a clean log is exactly the wedge
    # shape, so label it as such rather than folding it into "unclassified".
    if settled:
        end_idx, _ = alsa.find_card()
        if end_idx is None or not alsa.control_is_live(end_idx):
            settled = False
            shape = "settled_but_dead_at_end"

    result = {"attempts": attempts, "settled": settled, "shape": shape,
              "usb_core_retries": usb_core_retries}
    if trace_dir:
        # Saved after wait_for_settle()'s sleep completes, deliberately --
        # see issue48_settle_sweep.py's run_value() for why reading the
        # trace file before that would itself perturb the measurement.
        outcome_tag = "ok" if (settled and attempts == 1 and not usb_core_retries) \
            else (shape or "unknown")
        dest = os.path.join(trace_dir, f"{arm}-cycle{i}-{outcome_tag}.trace")
        save_trace(dest)
        result["trace"] = dest
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", required=True, choices=["idle", "active", "streaming"])
    ap.add_argument("--cycles", type=int, default=30)
    ap.add_argument("--off-seconds", type=float, default=2.0)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--race-ms-min", type=float, default=20)
    ap.add_argument("--race-ms-max", type=float, default=120)
    ap.add_argument("--rates", default="44100,48000,96000")
    ap.add_argument("--out")
    ap.add_argument("--save-trace",
                     help="directory to save each cycle's ftrace buffer into "
                          "(reuses #48's issue48_trace_enabled instrumentation "
                          "-- module must already be loaded with it set to 1)")
    ap.add_argument("--no-power-cycle-first", action="store_true",
                     help="skip the startup mains power cycle (default: on). "
                          "Only skip this deliberately -- e.g. to continue "
                          "investigating a state a previous run already left "
                          "the device in.")
    args = ap.parse_args()

    ok, why = priv.available()
    if not ok:
        sys.exit(f"cannot use the privileged helper: {why}")
    if not priv.usb_power_available():
        sys.exit("no Jockey 3 behind a ppps-capable hub port")

    if args.save_trace:
        os.makedirs(args.save_trace, exist_ok=True)

    if not args.no_power_cycle_first:
        # A real mains power cycle, not priv.usb_power() -- see the module
        # docstring for why the distinction matters here specifically. This
        # is about the device's *firmware* starting from a known-clean
        # state every run, independent of whatever wedge or partial-failure
        # state the previous run (or a previous cycle within this run) may
        # have left it in -- Frank's call, after #50 surfaced mid-run.
        print("== power-cycling the device before this run (--no-power-cycle-first to skip) ==")
        ok, detail = power.cycle()
        if not ok:
            sys.exit(f"startup power cycle failed: {detail}")
        idx = wait_for_card(True, 30.0)
        if idx is None:
            sys.exit("device did not reappear within 30s after the startup power cycle")
        print(f"device back as hw:{idx}, starting run\n")

    rates = [int(r) for r in args.rates.split(",")]
    race_lo = args.race_ms_min / 1000.0
    race_hi = args.race_ms_max / 1000.0

    print(f"== arm={args.arm} cycles={args.cycles} off_seconds={args.off_seconds} "
          f"timeout={args.timeout} ==")
    results = []
    for i in range(args.cycles):
        r = run_cycle(i, args.arm, rates, args.off_seconds, args.timeout,
                       race_lo, race_hi, args.save_trace)
        results.append(r)
        if r.get("error"):
            print(f"  cycle {i + 1}/{args.cycles}: ERROR: {r['error']}",
                  file=sys.stderr)
        else:
            status = "settled" if r["settled"] else "TIMED OUT"
            shape = f" shape={r['shape']}" if r.get("shape") else ""
            usb_core = f" usb_core_retries={r['usb_core_retries']}" \
                if r.get("usb_core_retries") else ""
            print(f"  cycle {i + 1}/{args.cycles}: {r['attempts']} attempt(s), "
                  f"{status}{shape}{usb_core}")

    ok_results = [r for r in results if r.get("attempts") is not None]
    if ok_results:
        attempts = [r["attempts"] for r in ok_results]
        # attempts > 1 misses a cycle whose only failure was USB-core-level
        # (device descriptor read), since that never produces a "probe ...
        # failed" line and so never increments `attempts` -- checked here too.
        flapped = sum(1 for r in ok_results
                      if r["attempts"] > 1 or r.get("usb_core_retries"))
        timed_out = sum(1 for r in ok_results if not r["settled"])
        shapes = {}
        for r in ok_results:
            if r.get("shape"):
                shapes[r["shape"]] = shapes.get(r["shape"], 0) + 1
        total_usb_core_retries = sum(r.get("usb_core_retries", 0) for r in ok_results)
        print(f"\n== summary ==")
        print(f"arm={args.arm}: {flapped}/{len(ok_results)} cycles flapped, "
              f"attempts min={min(attempts)} max={max(attempts)}, "
              f"{timed_out} timed out, "
              f"{len(results) - len(ok_results)} errored")
        if shapes:
            print("failure shapes (first failure per cycle): "
                  + ", ".join(f"{k}={v}" for k, v in sorted(shapes.items())))
        if total_usb_core_retries:
            print(f"usb_core_descriptor_read retries (total across all cycles): "
                  f"{total_usb_core_retries}")
    else:
        print("\nno usable cycles", file=sys.stderr)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"arm": args.arm, "results": results}, f, indent=2)
        print(f"\nfull results written to {args.out}")


if __name__ == "__main__":
    main()
