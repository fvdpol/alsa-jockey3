#!/usr/bin/env python3
"""Replace the commit message of patch <k> on the series branch with <msgfile>, and
rewrite the patches after it unchanged. The trees stay as they are."""
import argparse
from serieslib import rewrite

p = argparse.ArgumentParser()
p.add_argument('k', type=int)
p.add_argument('msgfile')
p.add_argument('--branch', default='series/v6')
p.add_argument('--dry-run', action='store_true')
a = p.parse_args()
msg = open(a.msgfile).read()
assert 'Signed-off-by:' in msg, 'message has no Signed-off-by:'
rewrite(a.branch, a.k, lambda c: {}, a.dry_run, {a.k: msg})
