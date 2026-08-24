#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""What is true of THIS machine, read from one file outside the repository.

Three kinds of setting have no business in the working tree. Addresses, like
the mains relay that switches the device. Paths, like where a built module is
fetched from. And what is physically attached, which differs per bench. All of
them are properties of a machine, and this driver is headed for mainline: a
personal hostname or home directory committed here would be wrong on its face.

The tree is also a Seafile share -- ~/jockey3_linux is a symlink into it -- so
a "local" file placed inside tests/hw does not stay local. It syncs, and the
EliteDesk's settings land on the Pi. That is why this lives under
~/.config/jockey3 and why it is one file rather than several: fewer things to
remember, fewer to forget.

    ~/.config/jockey3/machine.yaml

    version: 1

    capabilities:          # declared-only; can subtract, never add
      loopback-cable: true
      speakers: true

    power_switch:          # see lib/power/
      type: tasmota
      url: http://relay.example.com

    audio_loopback:         # see cases/audio_loopback.py (JT-AUDIO-002)
      patches:
        master: in1          # Master out -> In 1
        headphone: in2        # Headphone out -> In 2

    paths:
      build_host: alsa-dev
      build_path: ~/kbuild/{target}/sound/usb/jockey3/snd-reloop-jockey3.ko

    profiles:
      default: smoke
      applicable: [smoke, functional, regression, soak]

PRECEDENCE
----------
Environment first, file second, absent third. An environment variable has to
win, because the whole point of one is overriding the file for a single run
without editing it -- and a run that needs three exported variables to start
is a run that does not get made.

Absence is not an error anywhere here. It means this machine cannot do that
thing, and the caller decides what that costs.

BACK COMPATIBILITY
------------------
~/.config/jockey3/capabilities.yaml is still read when machine.yaml is
absent. It was the original name, machines are already set up with it, and
renaming a file is not worth a broken bench.
"""

import os
import sys

# This module doubles as a CLI (see _main() / reload_driver.sh), and when
# python3 runs it by path, sys.path[0] is this file's own directory rather
# than tests/hw -- so "from lib import yamlio" below would fail to find the
# lib package unless tests/hw is added explicitly, same as the other
# lib-importing entry-point scripts under actions/ and cases/ do.
sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from lib import yamlio    # noqa: E402

DEFAULT_DIR = "~/.config/jockey3"
FILENAME = "machine.yaml"
LEGACY_FILENAME = "capabilities.yaml"

_cache = {}


def path():
    """The config file this machine uses, whether or not it exists.

    JOCKEY3_CAPABILITIES is honoured for the legacy name so that machines
    pointing at a file by hand keep working.
    """
    override = os.environ.get("JOCKEY3_MACHINE_CONF") or \
        os.environ.get("JOCKEY3_CAPABILITIES")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    root = os.path.expanduser(DEFAULT_DIR)
    new = os.path.join(root, FILENAME)
    if os.path.exists(new):
        return os.path.abspath(new)
    legacy = os.path.join(root, LEGACY_FILENAME)
    if os.path.exists(legacy):
        return os.path.abspath(legacy)
    return os.path.abspath(new)


def load(refresh=False):
    """Parse the config. Returns (data, note); a missing file is ({}, None)."""
    p = path()
    if not refresh and p in _cache:
        return _cache[p]
    if not os.path.exists(p):
        result = ({}, None)
    elif not yamlio.available():                     # pragma: no cover
        result = ({}, f"PyYAML missing, cannot read {p}")
    else:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = yamlio.safe_load(f) or {}
            result = (data if isinstance(data, dict) else {},
                      None if isinstance(data, dict)
                      else f"{p}: top level is not a mapping")
        except (OSError, ValueError) as e:
            result = ({}, f"{p} is unreadable: {e}")
    _cache[p] = result
    return result


def section(name):
    """One top-level section as a mapping. Never raises, never returns None."""
    data, _note = load()
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def get(dotted, env=None, default=None):
    """One setting, environment first.

    `dotted` is "section.key"; `env` names an environment variable that
    overrides it. Paths are expanded, because every path in this file is one
    somebody typed with a ~ in it.
    """
    if env:
        value = os.environ.get(env)
        if value:
            return os.path.expanduser(value)
    part, _, key = dotted.partition(".")
    value = section(part).get(key, default)
    if isinstance(value, str) and value.startswith("~"):
        return os.path.expanduser(value)
    return value


def _main(argv):
    """Query the config from a shell script.

        machineconf.py get paths.build_host [env-var] [default]

    Prints the value and exits 0, or prints nothing and exits 1 when unset --
    so a caller can use the exit status and does not have to parse an error
    out of stdout.
    """
    if len(argv) < 2 or argv[0] != "get":
        print("usage: machineconf.py get <section.key> [ENV_VAR] [default]",
              file=sys.stderr)
        return 2
    value = get(argv[1],
                env=argv[2] if len(argv) > 2 else None,
                default=argv[3] if len(argv) > 3 else None)
    if value is None or value == "":
        return 1
    print(value)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
