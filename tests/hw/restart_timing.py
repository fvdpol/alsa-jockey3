#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Build and query the URB-restart timing dataset.

    ./restart_timing.py ingest [RUN_ID ...]   # add runs to data/restart_timing.json
    ./restart_timing.py report [--percentiles 50,90,95,99] [--split dyndbg]
    ./restart_timing.py sources                # what has been ingested
    ./restart_timing.py rebuild [--reparse]    # recompute from stored sources
                                               # (--reparse: re-read run folders)

A RUN_ID is "<target>/<run-dir>" or just "<run-dir>". With no RUN_ID, `ingest`
scans results/ for prod-kernel runs it has not seen. See lib/restart_timing.py
for what is stored and why.
"""

import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lib import restart_timing as rt  # noqa: E402
from lib import results as _results  # noqa: E402

RESULTS = _results.results_root()


def _run_dirs(run_ids):
    if run_ids:
        for rid in run_ids:
            hits = glob.glob(os.path.join(RESULTS, rid)) + \
                glob.glob(os.path.join(RESULTS, "*", rid))
            for h in hits:
                if os.path.isfile(os.path.join(h, "run.json")):
                    yield h
        return
    for p in sorted(glob.glob(os.path.join(RESULTS, "*", "*", "run.json"))):
        yield os.path.dirname(p)


def _read_run(run_dir):
    with open(os.path.join(run_dir, "run.json"), "r", encoding="utf-8") as f:
        run_json = json.load(f)
    dmesg_path = os.path.join(run_dir, "dmesg.txt")
    dmesg = ""
    if os.path.exists(dmesg_path):
        with open(dmesg_path, "r", encoding="utf-8") as f:
            dmesg = f.read()
    return run_json, dmesg


def cmd_ingest(args):
    data = rt.load()
    added = skipped = updated = 0
    for run_dir in _run_dirs(args.run_id):
        run_json, dmesg = _read_run(run_dir)
        rid = run_json.get("run_id")
        if not args.force and rt.has_run(data, rid):
            continue
        record, reason = rt.source_from_run(run_json, dmesg)
        if record is None:
            skipped += 1
            if args.verbose:
                print(f"  skip {rid}: {reason}")
            continue
        was = rt.has_run(data, rid)
        if rt.add_source(data, record):
            n = sum(sum(b.values()) for b in record["hist"].values())
            print(f"  {'update' if was else 'add'} {rid}  ({n} samples)")
            updated += was
            added += not was
    if added or updated:
        rt.save(data)
    print(f"\n{added} added, {updated} updated, {skipped} skipped; "
          f"{len(data['sources'])} sources total -> {rt.DATASET}")
    return 0


def cmd_sources(_args):
    data = rt.load()
    for s in data["sources"]:
        n = sum(sum(b.values()) for b in s["hist"].values())
        print(f"{s['run_id']:<48} {s['arch']:<8} {s.get('driver_git') or '?':<10} "
              f"dyndbg={s['dyndbg']:<4} {n:>5} samples  "
              f"[{', '.join(sorted(s['hist']))}]")
    print(f"\n{len(data['sources'])} sources")
    return 0


def cmd_report(args):
    data = rt.load()
    if not data["sources"]:
        print("no sources ingested yet -- run: ./restart_timing.py ingest")
        return 0
    ps = tuple(int(x) for x in args.percentiles.split(","))
    dims = ["arch", "stream", "start_type"]
    if args.split:
        dims += [d for d in args.split.split(",") if d not in dims]

    agg = rt.aggregate(data, dims=tuple(dims))
    hdr = " | ".join(dims)
    print(f"{hdr} :  n   " + "  ".join(f"p{p}" for p in ps) + "   min  max   (ms)")
    print("-" * (len(hdr) + 40))
    for key in sorted(agg, key=lambda k: tuple(str(x) for x in k)):
        bins = agg[key]
        st = rt.stats(bins, ps)
        start_type = key[dims.index("start_type")]
        ceil = rt.grace_ceilings(data, start_type)
        flag = "  <-- tail censored at grace %s" % ceil \
            if rt.tail_is_censored(bins, ceil) else ""
        cells = "  ".join(f"{st.get('p%d' % p, '-'):>3}" for p in ps)
        print(f"{' | '.join(str(x) for x in key)} : {st['n']:>3}  {cells}   "
              f"{st['min']:>3}  {st['max']:>3}{flag}")
    print(f"\n{len(data['sources'])} source run(s); dyndbg=on only "
          f"(see lib/restart_timing.py). Percentiles are nearest-rank.")
    return 0


def cmd_rebuild(args):
    data = rt.load()
    if args.reparse:
        kept = []
        for s in data["sources"]:
            hits = list(_run_dirs([s.get("run_path") or s["run_id"],
                                   s["run_id"].split("/")[-1]]))
            if not hits:
                print(f"  keep as-is (folder gone): {s['run_id']}")
                kept.append(s)
                continue
            run_json, dmesg = _read_run(hits[0])
            record, reason = rt.source_from_run(run_json, dmesg)
            if record is None:
                print(f"  drop {s['run_id']}: {reason}")
                continue
            kept.append(record)
            print(f"  reparsed {s['run_id']}")
        data["sources"] = sorted(kept, key=lambda s: s["run_id"])
        rt.save(data)
    print(f"{len(data['sources'])} sources; extractor v{rt.EXTRACTOR_VERSION}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest")
    p.add_argument("run_id", nargs="*")
    p.add_argument("--force", action="store_true", help="re-ingest even if already present")
    p.add_argument("--verbose", "-v", action="store_true", help="say why runs are skipped")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("report")
    p.add_argument("--percentiles", default="50,90,95,99")
    p.add_argument("--split", help="extra dims, comma-separated: dyndbg,kernel_release,target,driver_git")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("sources")
    p.set_defaults(fn=cmd_sources)

    p = sub.add_parser("rebuild")
    p.add_argument("--reparse", action="store_true",
                   help="re-read each source run folder (needed after the extractor changes)")
    p.set_defaults(fn=cmd_rebuild)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
