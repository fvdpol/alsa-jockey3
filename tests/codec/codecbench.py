#!/usr/bin/env python3
#
# Build, validate and benchmark the Ploytec codec in user space.
#
# Objective:
#   Give the codec a workbench: prove the shipped implementations correct on
#   whatever hardware is to hand, measure them honestly, and provide a place
#   to develop new ones before any of it goes near the driver.
#
#   Three things make that possible without touching the driver source:
#
#   1. The driver's ploytec_codec.c is COPIED here verbatim on every build,
#      never edited and never symlinked, with a SHA-256 manifest recorded
#      alongside. "check-sync" fails if the copy has drifted, so a benchmark
#      number can never be attributed to the wrong revision.
#
#   2. The driver compiles exactly one of its three codec variants, selected
#      by a preprocessor chain. To get all three into one binary, the same
#      copied file is compiled three times with different CONFIG_* defines,
#      renaming its three public symbols per variant on the command line.
#      No exports, no #ifdefs and no test hooks are added to the driver.
#
#   3. A generated registry (variants.gen.c) lists the driver variants and
#      everything in candidates/, so the C harness never hardcodes a list.
#
# (C) 2026 Frank van de Pol <fvdpol@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER_DIR = os.path.normpath(os.path.join(HERE, "..", ".."))
BUILD_DIR = os.path.join(HERE, "build")
SRC_COPY_DIR = os.path.join(BUILD_DIR, "src")
MANIFEST = os.path.join(SRC_COPY_DIR, "MANIFEST.json")
HARNESS_DIR = os.path.join(HERE, "harness")
STUBS_DIR = os.path.join(HARNESS_DIR, "stubs")
CANDIDATE_DIR = os.path.join(HERE, "candidates")
BINARY = os.path.join(BUILD_DIR, "test_codec")

# Copied verbatim from the driver. ploytec_codec_test_vectors.h is generated
# but lives with the driver because the KUnit suite consumes it too.
DRIVER_SOURCES = [
    "ploytec_codec.c",
    "ploytec_codec.h",
    "ploytec_codec_test_vectors.h",
]

# The driver's three public symbols, renamed per variant so all three
# implementations can coexist in one binary.
PUBLIC_SYMBOLS = [
    "ploytec_initialize_codec",
    "ploytec_encode_batch",
    "ploytec_decode_batch",
]

# name -> (CONFIG defines, human description)
VARIANTS = {
    "reference": (
        ["CONFIG_SND_USB_JOCKEY3_REFERENCE_CODEC=1"],
        "portable reference codec (CONFIG_SND_USB_JOCKEY3_REFERENCE_CODEC=y)",
    ),
    "opt64": (
        ["CONFIG_64BIT=1"],
        "optimized codec, 64-bit bit-spread table and magic-multiplier gather",
    ),
    "opt32": (
        [],
        "optimized codec, 32-bit bit-spread tables and nibble gather",
    ),
}

# Which variant the harness uses as its oracle.
REFERENCE_VARIANT = "reference"


def run(cmd, **kwargs):
    """Run a command, echoing it when it fails."""
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"command failed: {' '.join(cmd)}", file=sys.stderr)
    return result.returncode


def sha256_of(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def driver_git_commit():
    try:
        out = subprocess.run(["git", "-C", DRIVER_DIR, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, check=True)
        dirty = subprocess.run(["git", "-C", DRIVER_DIR, "status", "--porcelain",
                                "--"] + DRIVER_SOURCES,
                               capture_output=True, text=True, check=True)
        return out.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def copy_driver_sources():
    """Copy the driver codec here verbatim and record what was copied."""
    os.makedirs(SRC_COPY_DIR, exist_ok=True)

    entries = {}
    for name in DRIVER_SOURCES:
        source = os.path.join(DRIVER_DIR, name)
        if not os.path.exists(source):
            print(f"missing driver source: {source}", file=sys.stderr)
            if name == "ploytec_codec_test_vectors.h":
                print("run ./genvectors.py first", file=sys.stderr)
            sys.exit(1)

        # Plain copy, not copy2: preserving the mtime would make the copy
        # older than the previous build's objects and make would skip the
        # rebuild, silently testing the previous revision.
        shutil.copyfile(source, os.path.join(SRC_COPY_DIR, name))
        entries[name] = sha256_of(source)

    manifest = {
        "driver_dir": DRIVER_DIR,
        "driver_commit": driver_git_commit(),
        "sources": entries,
    }
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")

    return manifest


def check_sync(quiet=False):
    """Verify the copies still match the driver. Returns True when in sync."""
    if not os.path.exists(MANIFEST):
        if not quiet:
            print("no build/src/MANIFEST.json -- run './codecbench.py build' first")
        return False

    with open(MANIFEST, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    ok = True
    for name, recorded in manifest["sources"].items():
        source = os.path.join(DRIVER_DIR, name)
        if not os.path.exists(source):
            print(f"  {name}: gone from the driver")
            ok = False
            continue
        current = sha256_of(source)
        if current != recorded:
            print(f"  {name}: driver has changed since the last build")
            print(f"      built from {recorded[:16]}, driver now {current[:16]}")
            ok = False

    if not quiet:
        if ok:
            print(f"build/src is in sync with the driver "
                  f"({manifest['driver_commit']})")
        else:
            print("build/src is STALE -- rerun './codecbench.py build'")

    return ok


def discover_candidates():
    """Find candidates/*.c. The filename is the implementation name."""
    found = []
    for path in sorted(glob.glob(os.path.join(CANDIDATE_DIR, "*.c"))):
        name = os.path.splitext(os.path.basename(path))[0]
        if not name.isidentifier():
            print(f"skipping {path}: '{name}' is not a valid C identifier",
                  file=sys.stderr)
            continue
        found.append((name, path))
    return found


def generate_registry(candidates):
    """Emit variants.gen.c: extern declarations plus the registry array."""
    lines = [
        "// SPDX-License-Identifier: GPL-2.0-or-later",
        "/*",
        " * GENERATED FILE - DO NOT EDIT.",
        " *",
        " * Written by codecbench.py. Lists every codec implementation the",
        " * harness drives: the driver variants, each built from the same",
        " * verbatim copy of ploytec_codec.c with different CONFIG_* defines",
        " * and its public symbols renamed, followed by candidates/.",
        " */",
        "",
        '#include "codec_impl.h"',
        "",
    ]

    for variant in VARIANTS:
        for symbol in PUBLIC_SYMBOLS:
            renamed = f"{variant}_{symbol}"
            if symbol == "ploytec_initialize_codec":
                lines.append(f"int {renamed}(void);")
            elif symbol == "ploytec_encode_batch":
                lines.append(f"void {renamed}(u8 *dest, const u8 *src, const int n_frames);")
            else:
                lines.append(f"void {renamed}(u8 *dest, const u8 *src, const int n_frames);")
        lines.append("")

    # The driver's init returns an enum; wrap it to match the registry's
    # void(void) init signature.
    for variant in VARIANTS:
        lines += [
            f"static void {variant}_init_wrapper(void)",
            "{",
            f"\t{variant}_ploytec_initialize_codec();",
            "}",
            "",
        ]

    for name, _ in candidates:
        lines += [
            f"void {name}_init(void);",
            f"void {name}_encode_batch(u8 *dest, const u8 *src, const int n_frames);",
            f"void {name}_decode_batch(u8 *dest, const u8 *src, const int n_frames);",
            "",
        ]

    lines.append("const struct ploytec_codec_impl ploytec_impls[] = {")
    for variant, (_, description) in VARIANTS.items():
        lines += [
            "\t{",
            f'\t\t.name = "{variant}",',
            f'\t\t.description = "{description}",',
            "\t\t.kind = PLOYTEC_IMPL_DRIVER,",
            f"\t\t.init = {variant}_init_wrapper,",
            f"\t\t.encode = {variant}_ploytec_encode_batch,",
            f"\t\t.decode = {variant}_ploytec_decode_batch,",
            "\t},",
        ]
    for name, path in candidates:
        rel = os.path.relpath(path, HERE)
        lines += [
            "\t{",
            f'\t\t.name = "{name}",',
            f'\t\t.description = "candidate from {rel}",',
            "\t\t.kind = PLOYTEC_IMPL_CANDIDATE,",
            f"\t\t.init = {name}_init,",
            f"\t\t.encode = {name}_encode_batch,",
            f"\t\t.decode = {name}_decode_batch,",
            "\t},",
        ]
    lines.append("};")
    lines.append("")
    lines.append("const unsigned int ploytec_impl_count = "
                 "sizeof(ploytec_impls) / sizeof(ploytec_impls[0]);")
    lines.append(f'const char *ploytec_reference_impl = "{REFERENCE_VARIANT}";')
    lines.append("")

    target = os.path.join(BUILD_DIR, "variants.gen.c")
    with open(target, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return target


def compile_one(cc, cflags, source, output, extra=None):
    cmd = [cc] + cflags + (extra or []) + ["-c", source, "-o", output]
    return run(cmd)


def build(args):
    manifest = copy_driver_sources()
    os.makedirs(BUILD_DIR, exist_ok=True)

    cc = args.cc
    if args.cross:
        cc = f"{args.cross}-gcc"

    cflags = [
        "-O2",
        "-Wall",
        "-Wextra",
        "-Wno-unused-parameter",
        "-std=gnu11",
        f"-isystem{STUBS_DIR}",
        f"-I{SRC_COPY_DIR}",
        f"-I{HARNESS_DIR}",
        # Exactly what kbuild force-includes, so the driver's IS_ENABLED()
        # chain selects the same variant it would in the kernel.
        "-include", os.path.join(STUBS_DIR, "kbuild_shim.h"),
    ]
    if args.static:
        cflags.append("-static")

    objects = []

    # One object per driver variant, all from the same copied file.
    codec_src = os.path.join(SRC_COPY_DIR, "ploytec_codec.c")
    for variant, (defines, _) in VARIANTS.items():
        obj = os.path.join(BUILD_DIR, f"ploytec_codec_{variant}.o")
        extra = [f"-D{d}" for d in defines]
        extra += [f"-D{sym}={variant}_{sym}" for sym in PUBLIC_SYMBOLS]
        if compile_one(cc, cflags, codec_src, obj, extra) != 0:
            return 1
        objects.append(obj)

    candidates = discover_candidates()
    for name, path in candidates:
        obj = os.path.join(BUILD_DIR, f"candidate_{name}.o")
        if compile_one(cc, cflags, path, obj) != 0:
            return 1
        objects.append(obj)

    registry = generate_registry(candidates)
    registry_obj = os.path.join(BUILD_DIR, "variants.gen.o")
    if compile_one(cc, cflags, registry, registry_obj) != 0:
        return 1
    objects.append(registry_obj)

    main_obj = os.path.join(BUILD_DIR, "main.o")
    if compile_one(cc, cflags, os.path.join(HARNESS_DIR, "main.c"), main_obj) != 0:
        return 1
    objects.append(main_obj)

    if run([cc] + (["-static"] if args.static else []) +
           ["-o", BINARY] + objects + ["-lm"]) != 0:
        return 1

    print(f"built {BINARY}")
    print(f"  driver commit: {manifest['driver_commit']}")
    print(f"  variants:      {', '.join(VARIANTS)}")
    print(f"  candidates:    {', '.join(n for n, _ in candidates) or '(none)'}")

    return 0


def ensure_built(args):
    if not os.path.exists(BINARY) or not check_sync(quiet=True):
        return build(args)
    return 0


def invoke(args, harness_args):
    cmd = []
    if args.run_under:
        cmd += args.run_under.split()
    cmd += [BINARY] + harness_args

    return subprocess.run(cmd).returncode


def cmd_build(args):
    return build(args)


def cmd_test(args):
    rc = ensure_built(args)
    if rc:
        return rc

    harness_args = ["test"]
    if args.only:
        harness_args += ["--only", args.only]

    return invoke(args, harness_args)


def parse_duration(text):
    """Accept 500ms, 30s, 5m or a bare number of seconds."""
    text = text.strip().lower()
    if text.endswith("ms"):
        return float(text[:-2]) / 1000.0
    if text.endswith("s"):
        return float(text[:-1])
    if text.endswith("m"):
        return float(text[:-1]) * 60.0
    return float(text)


def cmd_bench(args):
    rc = ensure_built(args)
    if rc:
        return rc

    duration = 1.0 if args.quick else parse_duration(args.duration)

    harness_args = ["bench", "--duration", f"{duration}",
                    "--repeats", str(args.repeats)]
    if args.only:
        harness_args += ["--only", args.only]
    if args.json:
        harness_args.append("--json")

    if args.run_under:
        print("NOTE: running under an emulator -- timings are meaningless.\n"
              "      Use a native run on real hardware for benchmark numbers.",
              file=sys.stderr)

    if not args.json:
        return invoke(args, harness_args)

    cmd = (args.run_under.split() if args.run_under else []) + [BINARY] + harness_args
    result = subprocess.run(cmd, capture_output=True, text=True)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        return result.returncode

    payload = json.loads(result.stdout)
    with open(MANIFEST, "r", encoding="utf-8") as f:
        payload["source"] = json.load(f)
    payload["host"] = {
        "machine": os.uname().machine,
        "node": os.uname().nodename,
        "release": os.uname().release,
    }
    payload["settings"] = {"duration_s": duration, "repeats": args.repeats}

    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    print(f"wrote {args.json}")

    return 0


def cmd_check_sync(args):
    return 0 if check_sync() else 1


def cmd_vectors(args):
    return run([sys.executable, os.path.join(HERE, "genvectors.py")])


def cmd_list(args):
    rc = ensure_built(args)
    if rc:
        return rc
    return invoke(args, ["list"])


def cmd_clean(args):
    if os.path.exists(BUILD_DIR):
        shutil.rmtree(BUILD_DIR)
        print(f"removed {BUILD_DIR}")
    return 0


def cmd_report(args):
    """Merge per-machine JSON results into a markdown table."""
    rows = []
    for path in args.results:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        host = payload.get("host", {})
        for entry in payload["results"]:
            rows.append({
                "machine": host.get("machine", "?"),
                "node": host.get("node", "?"),
                "impl": entry["name"],
                "enc": entry["encode_ns_per_frame"],
                "dec": entry["decode_ns_per_frame"],
                "enc_rsd": entry["encode_rsd_percent"],
                "dec_rsd": entry["decode_rsd_percent"],
            })

    if not rows:
        print("no results given", file=sys.stderr)
        return 1

    # Speedups are relative to the portable reference on the same machine.
    baselines = {}
    for row in rows:
        if row["impl"] == REFERENCE_VARIANT:
            baselines[(row["node"], row["machine"])] = (row["enc"], row["dec"])

    print("| Machine | Arch | Implementation | Encode ns/frame | Decode ns/frame "
          "| Encode speedup | Decode speedup | RSD |")
    print("|---|---|---|---|---|---|---|---|")
    for row in rows:
        base = baselines.get((row["node"], row["machine"]))
        if base:
            enc_speedup = f"{base[0] / row['enc']:.2f}x" if row["enc"] else "-"
            dec_speedup = f"{base[1] / row['dec']:.2f}x" if row["dec"] else "-"
        else:
            enc_speedup = dec_speedup = "-"
        rsd = max(row["enc_rsd"], row["dec_rsd"])
        print(f"| {row['node']} | {row['machine']} | {row['impl']} "
              f"| {row['enc']:.2f} | {row['dec']:.2f} "
              f"| {enc_speedup} | {dec_speedup} | {rsd:.1f}% |")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Build, validate and benchmark the Ploytec codec.")
    parser.add_argument("--cc", default="gcc", help="host compiler (default gcc)")
    parser.add_argument("--cross", help="cross-compile prefix, e.g. aarch64-linux-gnu")
    parser.add_argument("--static", action="store_true",
                        help="link statically, for copying to a test machine")
    parser.add_argument("--run-under",
                        help="run the binary under this, e.g. qemu-aarch64-static")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("build", help="copy the driver sources and compile").set_defaults(
        func=cmd_build)

    p = sub.add_parser("test", help="run the correctness suite")
    p.add_argument("--only", help="restrict to one implementation")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("bench", help="run benchmarks")
    p.add_argument("--duration", default="30s",
                   help="target time per measurement: 500ms, 30s, 5m (default 30s)")
    p.add_argument("--quick", action="store_true",
                   help="shorthand for --duration 1s")
    p.add_argument("--repeats", type=int, default=5,
                   help="measurements per implementation (default 5)")
    p.add_argument("--only", help="restrict to one implementation")
    p.add_argument("--json", metavar="FILE", help="also write results as JSON")
    p.set_defaults(func=cmd_bench)

    sub.add_parser("check-sync",
                   help="verify build/src still matches the driver").set_defaults(
        func=cmd_check_sync)
    sub.add_parser("vectors",
                   help="regenerate the golden test vectors").set_defaults(
        func=cmd_vectors)
    sub.add_parser("list", help="list the implementations").set_defaults(
        func=cmd_list)
    sub.add_parser("clean", help="remove the build directory").set_defaults(
        func=cmd_clean)

    p = sub.add_parser("report", help="merge JSON results into a markdown table")
    p.add_argument("results", nargs="+", help="JSON files from 'bench --json'")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args()

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
