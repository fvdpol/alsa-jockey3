# SPDX-License-Identifier: GPL-2.0-or-later
"""The framework's only route to root.

Every privileged operation goes through one root-owned helper script, invoked
via sudo. Nothing else in the framework may call sudo, write to a root-owned
path, or require being run as root -- if a case needs a new privileged
operation, it gets a new verb in tests/hw/priv/jockey3-testctl, reviewed as a
change to the privilege boundary rather than absorbed into a case.

The suite used to expect `sudo ./runner.py`, which meant every case ran as
root: playing audio, parsing YAML, writing results. That is a much larger
surface than the work actually requires, and it left result files owned by
root in a directory the user has to read.

Running as root still works -- the helper is called directly rather than
through sudo when euid is already 0 -- so an existing `sudo ./runner.py`
habit does not break.
"""

import os
import shutil
import subprocess

HELPER = "/usr/local/sbin/jockey3-testctl"


def _argv(verb, args):
    base = [HELPER, verb, *[str(a) for a in args]]
    if os.geteuid() == 0:
        return base
    return ["sudo", "-n", *base]


def call(verb, *args, timeout=120):
    """Run one privileged verb. Returns (rc, stdout, stderr).

    rc 125 means the helper could not be invoked at all, which is a setup
    problem rather than a test failure -- callers should surface it as
    blocked, not failed.
    """
    try:
        # stdin off the terminal: no verb reads it, and it stops `sudo`'s
        # use_pty from relaying (and, on a timeout kill, failing to restore)
        # the caller's terminal. See lib/kmsg.KmsgCapture for the same guard.
        p = subprocess.run(_argv(verb, args), capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"{verb} timed out after {timeout}s"
    except OSError as e:
        return 125, "", str(e)


def available():
    """Can privileged operations be performed without a password?

    Returns (ok, reason). The reason is written for someone who has just hit
    it and needs to know what to run, not for a log.
    """
    if not os.path.exists(HELPER):
        return False, (f"{HELPER} is not installed -- run "
                       f"`sudo tests/hw/priv/install.sh` on this machine")
    if os.geteuid() != 0 and shutil.which("sudo") is None:
        return False, "sudo is not installed"
    rc, _out, err = call("status", timeout=20)
    if rc == 0:
        return True, ""
    if "password" in (err or "").lower():
        return False, (f"{HELPER} needs a password -- the sudoers drop-in is "
                       f"missing; run `sudo tests/hw/priv/install.sh`")
    return False, f"{HELPER} status failed: {(err or '').strip()[:120]}"


def stale():
    """True when the installed helper differs from the one in the repository.

    Worth knowing: a verb added in the repository does nothing until it is
    installed, and the failure otherwise looks like a broken case.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(here, "..", "priv", "jockey3-testctl")
    try:
        with open(src, "rb") as a, open(HELPER, "rb") as b:
            return a.read() != b.read()
    except OSError:
        return False


# ------------------------------------------------------------------ verbs

def load_module(timeout=60):
    return call("load", timeout=timeout)


def unload_module(timeout=60):
    return call("unload", timeout=timeout)


def unbind(timeout=30):
    return call("unbind", timeout=timeout)


def bind(timeout=30):
    return call("bind", timeout=timeout)


def verb_argv(verb, *args):
    """The exact argv `call()` would run for a verb. For a caller that needs
    to stream a long-running verb through subprocess.Popen rather than wait for
    it (see lib/kmsg.KmsgCapture)."""
    return _argv(verb, args)


def dmesg_read(timeout=30):
    rc, out, _err = call("dmesg-read", timeout=timeout)
    return out.splitlines() if rc == 0 else []


def dmesg_mark(token, timeout=20):
    rc, _out, _err = call("dmesg-mark", token, timeout=timeout)
    return rc == 0


def dyndbg_firmware(on=True, timeout=20):
    rc, _out, err = call("dyndbg-firmware", "on" if on else "off",
                         timeout=timeout)
    return rc == 0, (err or "").strip()


def dyndbg_hwparams_n(on=True, timeout=20):
    rc, _out, err = call("dyndbg-hwparams-n", "on" if on else "off",
                         timeout=timeout)
    return rc == 0, (err or "").strip()


def printk_console(level, timeout=20):
    rc, _out, err = call("printk-console", level, timeout=timeout)
    return rc == 0, (err or "").strip()


def xrun_inject(pcm, timeout=20):
    """Force an xrun on the open substream named pcm ('pcm0p' or 'pcm0c').

    One write to CONFIG_SND_PCM_XRUN_DEBUG's xrun_injection file, calling
    snd_pcm_stop_xrun() directly -- a safe no-op if that substream is closed
    or not currently running. See JT-PCM-009 (cases/pcm_xrun_recovery.py).
    """
    return call("xrun-inject", pcm, timeout=timeout)


def trace_callbacks_start(timeout=20):
    """Begin function_graph tracing of the two URB completion handlers."""
    return call("trace-callbacks", "start", timeout=timeout)


def trace_callbacks_collect(timeout=30):
    """Stop tracing and return (rc, trace_text, err). Resets tracing state."""
    return call("trace-callbacks", "collect", timeout=timeout)


def trace_callbacks_stop(timeout=20):
    """Reset tracing state with no output -- the error-path cleanup call."""
    return call("trace-callbacks", "stop", timeout=timeout)


def usb_power(action, *args, timeout=60):
    """Switch the hub port the Jockey 3 is plugged into.

    The port is never named here. The helper resolves it from the device's own
    USB ids, so no argument this side can widen what gets switched -- see the
    usb-power verb for why that matters on a rig where the hub one level up
    carries the keyboard and mouse.
    """
    return call("usb-power", action, *args, timeout=timeout)


def usb_power_available():
    rc, _out, _err = call("usb-power", "status", timeout=30)
    return rc == 0


def rtcwake_mem(seconds, timeout=None):
    # The helper blocks for the whole suspend, so the timeout has to outlast
    # it by enough to cover a slow resume rather than by a fixed margin.
    return call("rtcwake-mem", seconds, timeout=timeout or (seconds + 180))
