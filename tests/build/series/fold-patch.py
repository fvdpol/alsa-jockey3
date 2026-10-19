#!/usr/bin/env python3
"""Apply one main commit's diff to every commit of a series branch from patch
<first> onwards, rewriting the chain.
usage: fold-patch.py <main-commit> <first-patch> [--branch series/v6] [--dry-run]"""
import argparse, os, subprocess, tempfile
from serieslib import git, rewrite

ap = argparse.ArgumentParser()
ap.add_argument('fix'); ap.add_argument('first', type=int)
ap.add_argument('--branch', default='series/v6'); ap.add_argument('--dry-run', action='store_true')
a = ap.parse_args()
diff = git('show', '--format=', a.fix)
files = git('show', '--format=', '--name-only', a.fix).split()


def new_files(c):
    with tempfile.TemporaryDirectory() as d:
        for f in files:
            os.makedirs(os.path.dirname(os.path.join(d, f)) or d, exist_ok=True)
            open(os.path.join(d, f), 'w').write(git('show', f'{c}:{f}'))
        open(os.path.join(d, 'fix.patch'), 'w').write(diff)
        subprocess.run(['git', 'apply', '-C1', '--whitespace=nowarn', 'fix.patch'],
                       cwd=d, check=True)
        return {f: open(os.path.join(d, f)).read() for f in files}


rewrite(a.branch, a.first, new_files, a.dry_run)
