# SPDX-License-Identifier: GPL-2.0-or-later
"""Shared YAML loading -- see safe_load().

Was duplicated as a local `import yaml` plus a one-line `load()`/`load_yaml()`
wrapper in six separate files (runner.py, ledger.py, selftest.py,
checklist.py, lib/capabilities.py, lib/machineconf.py). Centralized here so
there is one place that picks the fast loader, not six that would need to
agree on how.
"""
try:
    import yaml
except ImportError:                                  # pragma: no cover
    yaml = None

# yaml.safe_load() always uses PyYAML's pure-Python parser -- only an
# explicit Loader=yaml.CSafeLoader gets libyaml's C implementation, even when
# libyaml is installed. On a slow single-core target that gap is an order of
# magnitude: catalog.yaml (2008 lines) measured 53.2s via plain safe_load()
# against 5.8s via CSafeLoader on pi1test (697 BogoMIPS, single ARMv6 core),
# and rules.yaml (521 lines) 14.6s against 1.4s. Falls back to the plain
# SafeLoader where libyaml is not installed, so this behaves exactly like
# yaml.safe_load() everywhere CSafeLoader is unavailable.
_LOADER = (getattr(yaml, "CSafeLoader", None) or getattr(yaml, "SafeLoader", None)) \
    if yaml else None


def available():
    """False when PyYAML itself could not be imported."""
    return yaml is not None


def safe_load(f):
    """Like yaml.safe_load(f), but via the C loader when one is available."""
    if yaml is None:
        raise RuntimeError("PyYAML is not installed")
    return yaml.load(f, Loader=_LOADER)
