# SPDX-License-Identifier: GPL-2.0-or-later
"""Capture what produced a result.

A verdict without an environment is not evidence. This module answers four
questions, none of which the operator should be asked to answer by hand:

  which driver binary   srcversion, resolved to a git revision via a manifest
  which firmware        parsed from the driver's own probe-time debug message
  which machine         CPU, RAM, board, USB host controller, cpufreq governor
  which kernel flavor   read from the kernel config, not declared

None of it requires a change to the driver. That constraint is absolute: the
driver ships upstream and must contain nothing that exists for tests.
"""

import glob
import gzip
import os
import re
import subprocess

MODULE = "snd_reloop_jockey3"          # as it appears in sysfs
MODULE_DASH = "snd-reloop-jockey3"     # as modprobe spells it

HERE = os.path.dirname(os.path.abspath(__file__))
HW_DIR = os.path.normpath(os.path.join(HERE, ".."))
REPO_DIR = os.path.normpath(os.path.join(HW_DIR, "..", ".."))


def _run(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=kw.pop("timeout", 20), **kw).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _read(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


# --------------------------------------------------------------- driver identity

def build_id():
    """GNU build-id of the loaded module.

    This is the point of the whole exercise: it identifies the binary that is
    actually resident, which catches the reload that silently did not happen
    and left the previous build in place.

    The obvious candidate was /sys/module/<m>/srcversion, but that only exists
    when the kernel was built with CONFIG_MODULE_SRCVERSION_ALL, which Debian
    and the Raspberry Pi kernels do not set -- so it is absent on exactly the
    machines this has to work on. The build-id is an ELF note emitted by the
    linker regardless of kernel config, and the kernel exposes module notes
    under /sys/module/<m>/notes/, so it is available everywhere.
    """
    path = f"/sys/module/{MODULE}/notes/.note.gnu.build-id"
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return parse_build_id_note(data)


def parse_build_id_note(data):
    """Extract the hex build-id from a raw ELF note."""
    import struct
    if len(data) < 12:
        return None
    try:
        namesz, descsz, _ntype = struct.unpack_from("<III", data, 0)
    except struct.error:
        return None
    off = 12 + (namesz + 3) // 4 * 4
    desc = data[off:off + descsz]
    return desc.hex() if desc else None


def srcversion():
    """Source hash, when the kernel was built to emit it.

    Recorded when present as a secondary identifier, but never relied on --
    see build_id() for why.
    """
    return _read(f"/sys/module/{MODULE}/srcversion") or None


def module_loaded():
    return os.path.isdir(f"/sys/module/{MODULE}")


def manifest_dir():
    """Where build manifests live.

    Under the results root, not in the repository: the module is built on the
    build host and run on the test host, so the manifest has to reach the
    machine doing the testing. Putting it beside the results means it travels
    by the same shared directory the results do, with nothing extra to
    remember.
    """
    override = os.environ.get("JOCKEY3_MANIFEST_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    root = os.environ.get("JOCKEY3_RESULTS_DIR")
    if root:
        return os.path.join(os.path.abspath(os.path.expanduser(root)),
                            "manifests")
    return os.path.join(HW_DIR, "results", "manifests")


def lookup_manifest(key):
    """Resolve a build-id to the build that produced it.

    Written by the build step. Absent is not an error -- it means the module
    was built outside the flow, which is worth recording rather than hiding.
    """
    if not key:
        return None
    path = os.path.join(manifest_dir(), f"{key}.json")
    if not os.path.exists(path):
        return None
    import json
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def driver_info():
    bid = build_id()
    info = {
        "loaded": module_loaded(),
        "build_id": bid,
        "srcversion": srcversion(),
        "vermagic": _read(f"/sys/module/{MODULE}/version") or None,
        "build": lookup_manifest(bid),
    }
    if info["loaded"] and not bid:
        info["build_id_note"] = (
            "no build-id note for the loaded module -- cannot identify which "
            "binary is resident")
    if info["loaded"] and bid and not info["build"]:
        info["build_note"] = (
            f"no manifest for build-id {bid[:12]} -- module was built outside "
            f"the flow, so the git revision under test is unknown")
    return info


# ------------------------------------------------------------------- firmware

# The driver reads the firmware revision at probe and logs it, and nothing
# else. It is not in bcdDevice, and there is no sysfs attribute -- adding one
# would be a change to the driver made for the benefit of tests, which is not
# allowed. So we read the driver's own message.
FW_RE = re.compile(r"Firmware 0x([0-9a-fA-F]{2}) v(\d+)\.(\d+)\.(\d+)")

DYNDBG = "/sys/kernel/debug/dynamic_debug/control"


def enable_firmware_debug():
    """Turn on just the probe message that carries the firmware revision.

    Enabled per-format rather than for the whole module, so the log is not
    flooded by every other dev_dbg in the driver during the run.

    Must be called BEFORE the module is loaded -- the message is emitted once,
    at probe.
    """
    if not os.path.exists(DYNDBG):
        return False, "debugfs dynamic_debug not available"
    rule = f"format \"Firmware 0x%02x\" +p\n"
    try:
        with open(DYNDBG, "w", encoding="utf-8") as f:
            f.write(rule)
        return True, ""
    except OSError as e:
        return False, f"cannot write dynamic_debug control: {e}"


def firmware_from_log(lines):
    """Extract the firmware revision from captured kernel log lines.

    Returns None when the message is absent, which is a real condition worth
    surfacing -- it means dynamic debug was not enabled before the module was
    loaded, so the run does not know which firmware it tested. That is a
    defect in the run, not a detail to paper over with "unknown".
    """
    for line in lines:
        m = FW_RE.search(line)
        if m:
            return {
                "hw_id": f"0x{m.group(1).lower()}",
                "version": f"{m.group(2)}.{m.group(3)}.{m.group(4)}",
            }
    return None


def usb_device_info():
    """Locate the Jockey 3 on the USB bus."""
    for vid, pid, name in (("200c", "1037", "Jockey 3 Remix"),
                           ("200c", "1009", "Jockey 3 Master Edition")):
        for idv in glob.glob("/sys/bus/usb/devices/*/idVendor"):
            base = os.path.dirname(idv)
            if _read(idv).lower() != vid:
                continue
            if _read(os.path.join(base, "idProduct")).lower() != pid:
                continue
            return {
                "model": name,
                "id": f"{vid}:{pid}",
                "bcd_device": _read(os.path.join(base, "bcdDevice")) or None,
                "serial": _read(os.path.join(base, "serial")) or None,
                "speed": _read(os.path.join(base, "speed")) or None,
                "path": os.path.basename(base),
                "host_controller": usb_host_controller(base),
            }
    return None


def usb_host_controller(devpath):
    """Which HCD driver the device is behind.

    xhci on the EliteDesk, dwc2 on the Pi 4. These behave differently under
    load and on disconnect, so a result is not comparable across them.
    """
    try:
        real = os.path.realpath(devpath)
    except OSError:
        return None
    # Walk up to the root hub's parent, which is the controller platform/PCI
    # device, and read the driver it is bound to.
    p = real
    for _ in range(8):
        p = os.path.dirname(p)
        drv = os.path.join(p, "driver")
        if os.path.islink(drv):
            name = os.path.basename(os.path.realpath(drv))
            if name not in ("usb",):
                return name
    return None


# -------------------------------------------------------------------- machine

def kernel_config():
    """Return the running kernel's config as a dict, or {} if unavailable."""
    cfg = {}
    text = None
    if os.path.exists("/proc/config.gz"):
        try:
            with gzip.open("/proc/config.gz", "rt", errors="replace") as f:
                text = f.read()
        except OSError:
            text = None
    if text is None:
        release = os.uname().release
        for cand in (f"/boot/config-{release}", "/boot/config"):
            if os.path.exists(cand):
                text = _read(cand)
                break
    if not text:
        return cfg
    for line in text.splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            k, _, v = line.partition("=")
            cfg[k[len("CONFIG_"):]] = v.strip()
    return cfg


# Recorded as context for every run.
DEBUG_OPTIONS = ["KASAN", "KASAN_GENERIC", "PROVE_LOCKING", "DEBUG_OBJECTS",
                 "SND_PCM_XRUN_DEBUG", "DEBUG_KERNEL", "LOCKDEP",
                 "DEBUG_KMEMLEAK", "DEBUG_ATOMIC_SLEEP", "WQ_WATCHDOG"]

# The subset that actually distinguishes a debug build. DEBUG_KERNEL is on in
# most distribution kernels and SND_PCM_XRUN_DEBUG is wanted in both of our
# configurations, so neither is evidence of anything.
HEAVY_DEBUG_OPTIONS = ["KASAN", "KASAN_GENERIC", "PROVE_LOCKING", "LOCKDEP",
                       "DEBUG_KMEMLEAK", "DEBUG_ATOMIC_SLEEP",
                       "DEBUG_OBJECTS"]

# Canonical architecture tokens used by targets.yaml. uname -m and the kernel
# config disagree about spelling, so both are normalized onto these.
UNAME_ARCH = {
    "x86_64": "x86_64", "amd64": "x86_64",
    "i686": "i386", "i586": "i386", "i386": "i386",
    "aarch64": "arm64", "arm64": "arm64",
    "armv6l": "armhf", "armv7l": "armhf", "armhf": "armhf",
}

CONFIG_ARCH = [
    ("ARM64", "arm64"),      # must precede ARM: arm64 configs set both
    ("X86_64", "x86_64"),
    ("X86_32", "i386"),
    ("ARM", "armhf"),
]


def canon_arch(machine):
    return UNAME_ARCH.get(machine, machine)


def arch_from_config(cfg):
    for symbol, token in CONFIG_ARCH:
        if cfg.get(symbol) == "y":
            return token
    return None


def kernel_info():
    """The running kernel -- what an L3+ case is actually testing against."""
    u = os.uname()
    cfg = kernel_config()
    enabled = [o for o in DEBUG_OPTIONS if cfg.get(o) in ("y", "m")]
    return {
        "source": "runtime",
        "release": u.release,
        "version": u.version,
        "arch": canon_arch(u.machine),
        "uname_machine": u.machine,
        "config_source": ("/proc/config.gz" if os.path.exists("/proc/config.gz")
                          else f"/boot/config-{u.release}"),
        "config_available": bool(cfg),
        "debug_options": enabled,
    }


def kernel_tree_info(kernel_src=None):
    """The kernel tree being built in -- what an L1/L2 gate is testing.

    A build gate run on the build host is not testing that host: it is testing
    the configuration in the tree, which may well be for another architecture.
    So build-only runs identify their target from here rather than from uname.
    """
    src = kernel_src or os.environ.get("KERNEL_SRC") or os.path.expanduser("~/sound")
    cfgpath = os.path.join(src, ".config")
    if not os.path.exists(cfgpath):
        return None
    cfg = {}
    for line in _read(cfgpath).splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            k, _, v = line.partition("=")
            cfg[k[len("CONFIG_"):]] = v.strip()
    enabled = [o for o in DEBUG_OPTIONS if cfg.get(o) in ("y", "m")]
    release = _run(["make", "-s", "-C", src, "kernelrelease"], timeout=60)
    return {
        "source": "tree",
        "tree": src,
        "release": release.splitlines()[-1] if release else None,
        "arch": arch_from_config(cfg),
        # CONFIG_LOCALVERSION is quoted in the config file.
        "localversion": (cfg.get("LOCALVERSION") or "").strip('"'),
        "config_source": cfgpath,
        "config_available": True,
        "debug_options": enabled,
    }


def cpu_info():
    model, count, flags = None, 0, None
    text = _read("/proc/cpuinfo")
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k in ("model name", "Model", "Processor") and not model:
            model = v
        if k == "processor":
            count += 1
        if k == "Hardware" and not model:
            model = v
    if not model:
        model = _read("/proc/device-tree/model") or None
    gov = None
    govs = sorted(glob.glob(
        "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"))
    if govs:
        gov = _read(govs[0]) or None
    freq = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") or None
    return {
        "model": model,
        "cores": count or None,
        # Latency and jitter numbers cannot be compared between runs without
        # these. A governor change alone invalidates a metric series.
        "governor": gov,
        "cur_freq_khz": int(freq) if freq and freq.isdigit() else None,
    }


def memory_kb():
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


def board_model():
    dt = _read("/proc/device-tree/model")
    if dt:
        return dt.replace("\x00", "")
    for f in ("/sys/class/dmi/id/product_name", "/sys/class/dmi/id/board_name"):
        v = _read(f)
        if v:
            return v
    return None


def distro():
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.partition("=")[2].strip().strip('"')
    return None


def alsa_versions():
    return {
        "driver": _read("/proc/asound/version") or None,
        "aplay": (_run(["aplay", "--version"]) or "").strip() or None,
    }


def machine_info():
    return {
        "hostname": os.uname().nodename,
        "board": board_model(),
        "cpu": cpu_info(),
        "memory_kb": memory_kb(),
        "distro": distro(),
        "alsa": alsa_versions(),
    }


# ------------------------------------------------------------ target detection

def detect_target(targets, kernel=None):
    """Identify the target from what was built, not from which box we are on.

    Matching is on architecture plus the kernel's LOCALVERSION, which each
    configuration sets distinctively so that `uname -r` names its own target.
    Substring matching, because setlocalversion appends "+" to an untagged git
    tree and that should not change the answer.

    Deliberately derived rather than declared: the operator is the least
    reliable component in the system at 2am, and a soak run mislabeled as
    x86_64-prod when it was really x86_64-debug corrupts a metric series
    silently.
    """
    k = kernel or kernel_info()
    arch = k.get("arch")
    release = k.get("release") or ""
    lv = k.get("localversion") or ""

    matches = []
    for name, spec in targets.items():
        if spec.get("arch") != arch:
            continue
        want = spec.get("localversion")
        if not want:
            continue
        if want in release or (lv and want in lv):
            matches.append((name, spec))

    if len(matches) == 1:
        name, spec = matches[0]
        return name, spec, None
    if len(matches) > 1:
        return None, None, (
            "kernel release '%s' matches more than one target (%s); make the "
            "LOCALVERSION strings unambiguous" %
            (release, ", ".join(n for n, _ in matches)))

    # Say what it looks like, without acting on it. The config is a decent
    # hint and a poor identity: it cannot distinguish a deliberate debug
    # kernel from a distribution kernel that merely enables lockdep.
    heavy = [o for o in (k.get("debug_options") or [])
             if o in HEAVY_DEBUG_OPTIONS]
    looks = "debug" if heavy else "prod"
    guess = f"{arch}-{looks}" if arch else None
    hint = (f" Judging by its configuration this looks like '{guess}', but "
            f"that is a guess from the debug options alone, which is exactly "
            f"what LOCALVERSION exists to replace." if guess else "")
    return None, None, (
        f"kernel release '{release}' (arch {arch}) does not name a target. "
        f"Build each configuration with a distinctive LOCALVERSION -- set "
        f"CONFIG_LOCALVERSION=\"-alsa-debug\" or \"-alsa-prod\" in its .config "
        f"-- so the kernel identifies itself. Or pass --target explicitly."
        + hint)


def verify_target(spec, kernel):
    """Cross-check the configuration against what the target claims to be.

    LOCALVERSION is a label, and a label can be wrong. A kernel calling itself
    -alsa-prod with KASAN compiled in is a mislabeled build, and every timing
    number it produces would silently pollute the production series.
    """
    want = spec.get("verify") or {}
    enabled = kernel.get("debug_options") or []
    if not kernel.get("config_available"):
        return ["kernel config not readable, so the target label could not be "
                "cross-checked against the actual configuration"]
    problems = []
    missing = [o for o in want.get("kconfig_any", [])
               if o in enabled]
    if "kconfig_any" in want and not missing:
        problems.append(
            "target expects a debug kernel but none of "
            f"{', '.join(want['kconfig_any'])} is enabled")
    present = [o for o in want.get("kconfig_none", []) if o in enabled]
    if present:
        problems.append(
            f"target expects a production kernel but {', '.join(present)} "
            f"is enabled")
    return problems


def capture(targets=None, kernel_src=None):
    """The full environment record for a run.

    The machine block is context, not identity: which EliteDesk it was, how
    much memory it had, what the governor was doing. Useful when a number
    looks wrong, never part of what the run claims to have tested.
    """
    env = {
        "kernel": kernel_info(),
        "kernel_tree": kernel_tree_info(kernel_src),
        "machine": machine_info(),
        "driver": driver_info(),
        "device": usb_device_info(),
        "firmware": None,          # filled in once the probe log is captured
    }
    if targets:
        name, _spec, err = detect_target(targets)
        env["detected_target"] = name
        if err:
            env["detected_target_error"] = err
    return env
