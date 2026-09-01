#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
#
# ov-snapshot-trigger -- watch the kernel log on a device under test and poke
# ov-snapshot when an interesting line appears.
#
# Runs standalone on the DUT, independent of any test runner, so it also covers
# ad-hoc and overnight bring-up sessions. It tails `dmesg --follow`, matches
# each line against a list of regexes from the config, and POSTs /trigger to
# ov-snapshot with the facts a trace sidecar needs (which this side, not the
# capture host, is the one that knows).
#
# Standalone by design: stdlib only, no imports from any driver tree.
#
# (C) 2026 Frank van de Pol

import argparse
import json
import os
import platform
import re
import socket
import struct
import subprocess
import sys
import time
import tomllib
import urllib.request


def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    t = cfg.get("trigger", cfg)
    if not t.get("url"):
        sys.exit("config: trigger.url is required (e.g. http://capturehost:8464)")
    t["url"] = t["url"].rstrip("/")
    if not t.get("patterns"):
        sys.exit("config: trigger.patterns must list at least one regex")
    t.setdefault("module", "")
    t.setdefault("cooldown_seconds", 30.0)
    t.setdefault("application", "unknown / concurrent")
    t.setdefault("dmesg_command", ["dmesg", "--follow", "--decode"])
    t.setdefault("post_arm", True)
    return t


# --------------------------------------------------------------------- DUT facts

def _os_pretty():
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


def _build_id(module):
    """Hex GNU build-id of a loaded module, from its ELF note in sysfs.

    Same approach the jockey3 test suite uses -- srcversion is absent on Debian
    and Pi kernels, the build-id note is always there.
    """
    if not module:
        return None
    path = f"/sys/module/{module}/notes/.note.gnu.build-id"
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if len(data) < 12:
        return None
    namesz, descsz, _type = struct.unpack_from("<III", data, 0)
    off = 12 + (namesz + 3) // 4 * 4
    desc = data[off:off + descsz]
    return desc.hex() or None


def _kernel_config(release):
    if "-debug" in release:
        return "debug"
    if "-prod" in release:
        return "prod"
    return "unknown"


def gather_facts(cfg, line, pattern):
    release = platform.release()
    return {
        "tag": cfg.get("tag") or _tag_from(line),
        "kmsg_line": line.strip(),
        "matched_pattern": pattern,
        "host": socket.gethostname(),
        "os": _os_pretty(),
        "kernel": release,
        "kernel_config": _kernel_config(release),
        "build_id": _build_id(cfg["module"]),
        "application": cfg["application"],
        "objective": cfg.get("objective", ""),
        "client_wall": time.time(),
    }


def _tag_from(line):
    m = re.search(r"jockey3[:\s]+(.*)", line, re.I)
    text = (m.group(1) if m else line).strip()
    return re.sub(r"[^A-Za-z0-9]+", "-", text)[:40].strip("-") or "trigger"


# ------------------------------------------------------------------------- http

def post(url, obj=None, timeout=10):
    data = json.dumps(obj or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read() or b"{}")


# ------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config",
                    default=os.path.expanduser("~/.config/ov-snapshot/trigger.toml"))
    ap.add_argument("--dry-run", action="store_true",
                    help="match and print, but do not POST")
    args = ap.parse_args()

    cfg = load_config(args.config)
    patterns = [re.compile(p) for p in cfg["patterns"]]
    print(f"[trigger] capture host: {cfg['url']}")
    print(f"[trigger] patterns: {cfg['patterns']}")

    if cfg["post_arm"] and not args.dry_run:
        try:
            print("[trigger] arm ->", post(cfg["url"] + "/arm")[1])
        except Exception as e:
            print(f"[trigger] warning: could not arm capture host: {e}")

    proc = subprocess.Popen(cfg["dmesg_command"], stdout=subprocess.PIPE,
                            text=True, bufsize=1)
    last_fire = 0.0
    try:
        for line in proc.stdout:
            hit = next((p.pattern for p in patterns if p.search(line)), None)
            if not hit:
                continue
            now = time.monotonic()
            if now - last_fire < cfg["cooldown_seconds"]:
                print(f"[trigger] (cooldown) {line.strip()}")
                continue
            last_fire = now
            facts = gather_facts(cfg, line, hit)
            print(f"[trigger] MATCH {hit!r}: {line.strip()}")
            if args.dry_run:
                print("[trigger] dry-run, would POST:", json.dumps(facts))
                continue
            try:
                status, body = post(cfg["url"] + "/trigger", facts)
                print(f"[trigger] -> {status} {body}")
            except Exception as e:
                print(f"[trigger] POST failed: {e}")
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        if cfg["post_arm"] and not args.dry_run:
            try:
                post(cfg["url"] + "/disarm")
            except Exception:
                pass


if __name__ == "__main__":
    main()
