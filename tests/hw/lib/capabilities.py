# SPDX-License-Identifier: GPL-2.0-or-later
"""What this machine can actually do, right now.

A capability is a precondition a case needs from its surroundings: a device on
the bus, a loopback cable, a person at the keyboard. Cases declare what they
need in catalog.yaml; this module answers whether it is here.

WHY NOT IN targets.yaml
-----------------------
It used to be, and it was keyed wrong. A target is a *build* -- an architecture
paired with a kernel configuration -- and `x86_64-debug` and `x86_64-prod` are
the same EliteDesk booted differently. A loopback cable plugged into that
machine belongs to both, so it had to be declared twice and kept in sync by
hand. Capabilities belong to the machine; targets describe what was built.

Splitting them also fixes the churn: targets.yaml is meant to be static, and a
cable that is plugged in on Tuesday and coiled on the bench on Wednesday made
it anything but.

THREE SOURCES, IN ORDER OF PREFERENCE
-------------------------------------
probed       The machine is asked. Nothing to declare and nothing to forget:
             a missing package or an unplugged controller answers for itself.
declared     For things no probe can settle -- whether a cable is in the right
             socket, whether anyone is listening. Read from a small local file.
invocation   `human` comes from how the runner was started, not from the room.

A declaration can only ever take a capability AWAY. Declaring `usb-power:
false` when the hub is present is a useful opt-out ("it is plugged in, but do
not power-cycle today"); declaring `loopback-cable: true` when it is not
plugged in cannot make it so, and for probed capabilities the machine wins.

An absent file grants nothing. A fresh machine that silently claimed a
loopback cable it did not have would record a meaningless pass, which is worse
than recording nothing.
"""

import glob
import os
import shutil
import subprocess

from lib import env, machineconf, power, priv, yamlio

# Outside the repository on purpose. The working tree is a Seafile share --
# ~/jockey3_linux is a symlink into it -- so a "local" file inside tests/hw
# would sync to every machine, and the EliteDesk's capabilities would land on
# the Pi. Env var first, matching JOCKEY3_RESULTS_DIR and JOCKEY3_MANIFEST_DIR.
DEFAULT_PATH = "~/.config/jockey3/capabilities.yaml"

# Settled by asking the machine. Never declared, never in a file.
PROBED = ("device", "root", "kernel-tree", "cross-toolchains", "qemu", "sox",
          "python-mido", "rtc-wake", "usb-power", "gadget-emulation",
          "device-power")

# No probe exists, or none can exist. These come from the local file, and
# default to false when it is absent. mixxx/jackd/pipewire mean "set up and
# ready for an interop session on this bench", not merely "binary on PATH",
# so they are declared like the rest rather than probed with `which`.
DECLARED = ("loopback-cable", "signal-source", "speakers", "two-devices",
            "quiet-machine", "mixxx", "jackd", "pipewire")

# Supplied by how the runner was invoked.
INVOCATION = ("human",)

ALL = PROBED + DECLARED + INVOCATION


def config_path():
    return machineconf.path()


def load_declared(path=None):
    """Read the local declarations. Returns (mapping, note).

    A missing file is normal and not an error -- it means nothing extra is
    connected. A malformed one IS worth surfacing, because the failure it
    causes otherwise is a case mysteriously blocked.
    """
    if path:
        # An explicit path is still honoured for tests that write one.
        if not os.path.exists(path):
            return {}, None
        if not yamlio.available():                   # pragma: no cover
            return {}, f"PyYAML missing, cannot read {path}"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yamlio.safe_load(f) or {}
        except (OSError, ValueError) as e:
            return {}, f"{path} is unreadable: {e}"
    else:
        data, note = machineconf.load()
        if note:
            return {}, note
        path = machineconf.path()
    caps = data.get("capabilities")
    if caps is None:
        # Tolerate a bare mapping at the top level; the wrapper is a
        # convention, not a requirement worth failing over.
        caps = {k: v for k, v in data.items()
                if k not in ("version", "power_switch", "paths", "profiles")}
    if not isinstance(caps, dict):
        return {}, f"{path}: 'capabilities' is not a mapping"
    out, unknown = {}, []
    for k, v in caps.items():
        if k not in ALL:
            unknown.append(k)
            continue
        out[k] = bool(v)
    note = None
    if unknown:
        note = (f"{path}: ignoring unknown capability "
                + ", ".join(sorted(unknown)))
    return out, note


# ------------------------------------------------------------------- probes

def _which(*names):
    return all(shutil.which(n) for n in names)


def _probe_rtc_wake():
    # rtcwake lives in /sbin, which is not on a non-root PATH, so `which` says
    # missing on a machine where it works fine. Look for the binary directly,
    # and confirm the kernel will actually suspend to RAM and that there is
    # an RTC to arm a wake alarm in -- rtcwake needs both, and on the Pi test
    # fleet it is neither package nor probe that is missing, it is the
    # hardware: no onboard RTC, and no suspend-to-RAM support advertised by
    # the kernel. Returning *why* here, not just whether, is what lets the
    # runner's blocked-case message say that instead of the generic "needs
    # rtc-wake" that used to send people hunting for a nonexistent missing
    # Debian package.
    have_binary = any(os.path.exists(p) for p in
                      ("/usr/sbin/rtcwake", "/sbin/rtcwake", "/usr/bin/rtcwake"))
    try:
        with open("/sys/power/state", "r", encoding="utf-8") as f:
            mem = "mem" in f.read().split()
    except OSError:
        mem = False
    have_rtc = bool(glob.glob("/dev/rtc*"))

    if have_binary and mem and have_rtc:
        return True, None

    missing = []
    if not have_binary:
        missing.append("rtcwake is not installed")
    if not mem:
        missing.append("kernel advertises no suspend-to-RAM support "
                       "(/sys/power/state has no \"mem\" state)")
    if not have_rtc:
        missing.append("no RTC device (no /dev/rtc*) to arm a wake alarm in")
    return False, "; ".join(missing)


def _probe_python_mido():
    try:
        subprocess.run(["python3", "-c", "import mido"], check=True,
                       capture_output=True, timeout=30)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _probe_qemu():
    return bool(glob.glob("/usr/bin/qemu-system-*")
                or glob.glob("/usr/local/bin/qemu-system-*"))


def _probe_gadget_emulation():
    try:
        p = subprocess.run(["modinfo", "dummy_hcd"], capture_output=True,
                           timeout=30)
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _probe_device_power():
    """Control over the device's own mains supply.

    Named for what is controlled rather than for what does the controlling: it
    may be a relay today and a solid-state switch tomorrow, and no test has
    any business caring which. The protocol lives in lib/power/, behind a
    backend.

    Probed rather than declared: the switch either answers or it does not, and
    a declaration that it is present when the network is down would block
    JT-AUDIO-005 with a confusing error instead of demoting it to manual.
    """
    return power.available()


def _probe_usb_power():
    """Is a Jockey 3 sitting behind a switchable hub port?

    Asked through the privileged helper rather than by running uhubctl here:
    querying per-port power needs root, and the helper is the suite's only
    route to it. It answers for the device by its own USB ids, so a `yes` here
    means this exact controller can be power-cycled -- not merely that some
    ppps hub exists somewhere on the bus.
    """
    rc, _out, _err = priv.call("usb-power", "status", timeout=30)
    return rc == 0


PROBES = {
    "device": lambda: env.usb_device_info() is not None,
    "root": lambda: priv.available()[0],
    "kernel-tree": lambda: env.kernel_tree_info() is not None,
    "cross-toolchains": lambda: _which("aarch64-linux-gnu-gcc",
                                       "arm-linux-gnueabihf-gcc"),
    "qemu": _probe_qemu,
    "sox": lambda: _which("sox"),
    "python-mido": _probe_python_mido,
    "rtc-wake": _probe_rtc_wake,
    "usb-power": _probe_usb_power,
    "device-power": _probe_device_power,
    "gadget-emulation": _probe_gadget_emulation,
}


def resolve(attended=True, path=None, skip_probes=False):
    """Work out the capability set for this run.

    Returns (available, detail). `available` is the set of capability names
    that hold; `detail` maps every known name to what was decided and why,
    and belongs in the run record -- a JT-AUDIO-002 pass from a day the
    loopback cable was connected is otherwise indistinguishable from one taken
    with it coiled on the bench, and ledger.py exists to answer exactly that
    kind of question.
    """
    declared, note = load_declared(path)
    detail = {}

    for name in ALL:
        why = None
        if name in INVOCATION:
            value, source = attended, "invocation"
        elif name in PROBED:
            if skip_probes:
                value, source = False, "not probed"
            else:
                try:
                    result = PROBES[name]()
                except Exception:                     # noqa: BLE001
                    # A probe that throws must not take the run with it. It
                    # answers "no", which blocks the affected cases and says
                    # so, rather than aborting everything.
                    value, source = False, "probe failed"
                else:
                    # A probe may return a bare bool, or (bool, reason) when
                    # it has something specific to say about a "no" -- see
                    # _probe_rtc_wake().
                    if isinstance(result, tuple):
                        value, why = result
                    else:
                        value, why = result, None
                    value = bool(value)
                    source = "probed"
            # A declaration may veto a probe, never invent one.
            if value and declared.get(name) is False:
                value, source = False, "declared off"
                why = None
        else:
            value = declared.get(name, False)
            source = "declared" if name in declared else "default off"
        detail[name] = {"available": value, "source": source}
        if why:
            detail[name]["why"] = why

    if note:
        detail["_note"] = note
    available = {n for n, d in detail.items()
                 if isinstance(d, dict) and d["available"]}
    return available, detail


def example_file():
    """The contents of a starter capabilities file."""
    lines = [
        "# Capabilities of THIS machine that cannot be probed for.",
        "#",
        "# Everything else -- the device, root, sox, the ppps hub -- is",
        "# detected, so it does not belong here. `human` comes from whether",
        "# the runner was started with --unattended.",
        "#",
        "# Absent file or absent entry means false. Nothing here can grant a",
        "# capability the machine does not have; a `false` can withhold one",
        "# that it does (usb-power: false stops tests power-cycling the",
        "# device without unplugging the hub).",
        "version: 1",
        "capabilities:",
    ]
    for name in DECLARED:
        lines.append(f"  {name}:{' ' * (16 - len(name))}false")
    lines.append("  # usb-power is probed; set false only to veto it")
    return "\n".join(lines) + "\n"
