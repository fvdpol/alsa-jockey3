#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""L2: measure codec throughput and speedup over the reference implementation.

Metric-only -- must run on an otherwise idle machine (catalog.yaml's
`requires: [quiet-machine]`, enforced by the runner) and never under QEMU,
since emulated-CPU timings are meaningless. Wraps
tests/codec/codecbench.py, which already owns the build, the
source-sync guard and the driver/host metadata baked into its JSON output;
this case does not reimplement any of that, only the live-feedback polling
and the mbps/speedup derivation from ns_per_frame.

"mbps" here means megabytes/second (MB/s), not megabits. ns_per_frame is
per single PCM frame -- see harness/main.c's own "ns/frame is per PCM frame"
note -- so mbps = bytes_per_frame * 1000 / ns_per_frame, with the PCM frame
sizes taken from ploytec_codec.h (4 channels x 3 bytes encode, 6 channels x
3 bytes decode).

Every variant, including experimental candidates, is still benchmarked and
logged in full (codecbench.log, bench.json in the case workdir); this case
only extracts the two rows the metrics need: the portable reference as
baseline, and whichever optimized variant this host's word size means
CONFIG_SND_USB_JOCKEY3 would actually ship (opt64 on 64-bit, opt32 on
32-bit -- see docs/codec_testing.md), so the speedup is a claim about the
codec a real user runs, not just whichever variant happened to be built.
"""

import ctypes
import json
import os
import select
import subprocess
import sys
import time

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.case import Case          # noqa: E402

REFERENCE_VARIANT = "reference"
NOISY_RSD_PERCENT = 5.0
STATUS_INTERVAL_S = 5

# ploytec_codec.h: PLAYBACK_PCM_FRAME_SIZE (4ch * 3B) / CAPTURE_PCM_FRAME_SIZE (6ch * 3B)
ENC_PCM_BYTES = 12
DEC_PCM_BYTES = 18


def shipped_variant():
    """Which optimized codec CONFIG_SND_USB_JOCKEY3 selects on this host."""
    return "opt64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "opt32"


def main():
    c = Case()
    script = os.path.join(c.repo, "tests", "codec", "codecbench.py")
    if not os.path.exists(script):
        c.blocked(f"{script} not found")

    duration = float(c.params.get("duration_seconds", 30))
    repeats = int(c.params.get("repeats", 5))
    variant = shipped_variant()

    # Rough estimate matching codecbench's own: 4 shipped variants, two
    # directions each, 'repeats' measurements of 'duration' seconds.
    eta = 4 * 2 * repeats * duration

    json_path = os.path.join(c.workdir, "bench.json")
    c.progress(f"benchmarking all codec variants against golden vectors "
               f"(~{eta / 60:.0f} min estimate: {repeats} repeats of "
               f"{duration:.0f}s per direction)")

    start = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, script, "bench",
         "--duration", f"{duration}s", "--repeats", str(repeats),
         "--json", json_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    # codecbench.py prints a line per (variant, direction, repeat) -- with 4
    # variants x 2 directions x 'repeats' measurements of 'duration' seconds
    # each, a line can be minutes apart. A plain readline() blocks across
    # that whole gap, so the c.status() below it never ran and this case sat
    # silent for most of a 20-minute run. Bounding the wait with select()
    # instead means a status line is guaranteed every STATUS_INTERVAL_S
    # regardless of how quiet codecbench.py itself is.
    lines = []
    while True:
        ready, _w, _x = select.select([proc.stdout], [], [], STATUS_INTERVAL_S)
        if ready:
            line = proc.stdout.readline()
            if line:
                lines.append(line)
                continue
        if proc.poll() is not None:
            break
        elapsed = time.monotonic() - start
        c.status(f"benchmarking  {elapsed:.0f}s / ~{eta:.0f}s")
    proc.wait()

    log_path = os.path.join(c.workdir, "codecbench.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("".join(lines))

    if proc.returncode != 0:
        c.fail(f"codecbench.py bench exited {proc.returncode}, see {log_path}")
        c.done()

    if not os.path.exists(json_path):
        c.fail("codecbench.py exited 0 but wrote no JSON results")
        c.done()

    with open(json_path, encoding="utf-8") as f:
        payload = json.load(f)

    by_name = {r["name"]: r for r in payload.get("results", [])}
    base = by_name.get(REFERENCE_VARIANT)
    shipped = by_name.get(variant)

    if base is None or shipped is None:
        c.fail(f"missing '{REFERENCE_VARIANT}' or '{variant}' row in "
               f"results: got {sorted(by_name)}")
        c.done()

    enc_ns = shipped["encode_ns_per_frame"]
    dec_ns = shipped["decode_ns_per_frame"]
    enc_rsd = shipped["encode_rsd_percent"]
    dec_rsd = shipped["decode_rsd_percent"]

    encode_mbps = round(ENC_PCM_BYTES * 1000 / enc_ns, 1)
    decode_mbps = round(DEC_PCM_BYTES * 1000 / dec_ns, 1)
    encode_speedup = round(base["encode_ns_per_frame"] / enc_ns, 2)
    decode_speedup = round(base["decode_ns_per_frame"] / dec_ns, 2)

    c.metric("encode_mbps", encode_mbps)
    c.metric("decode_mbps", decode_mbps)
    c.metric("encode_speedup", encode_speedup)
    c.metric("decode_speedup", decode_speedup)
    c.metric("encode_rsd_percent", enc_rsd)
    c.metric("decode_rsd_percent", dec_rsd)

    # Metric-only per the catalog -- a noisy machine does not fail the case,
    # but a number nobody flagged as untrustworthy is worse than no number.
    for direction, rsd in (("encode", enc_rsd), ("decode", dec_rsd)):
        if rsd > NOISY_RSD_PERCENT:
            c.note(f"{variant} {direction} RSD {rsd:.1f}% > "
                   f"{NOISY_RSD_PERCENT:.0f}% -- machine was not quiet "
                   f"enough, raise duration_seconds or check for contention")

    c.progress(f"{variant}: encode {enc_ns:.2f} ns/frame "
               f"({encode_mbps:.1f} MB/s, {encode_speedup:.2f}x over "
               f"{REFERENCE_VARIANT}), decode {dec_ns:.2f} ns/frame "
               f"({decode_mbps:.1f} MB/s, {decode_speedup:.2f}x over "
               f"{REFERENCE_VARIANT})")

    c.done()


if __name__ == "__main__":
    main()
