#!/usr/bin/env python3
"""Like fold-patch.py, for a fix whose context differs between patches: apply
<fixer>.fix() (a str -> str function) to <file> in every commit of the series
branch from patch <first> onwards, rewriting the chain.
usage: fold-sub.py <fixer.py> <file> <first-patch> [--branch series/v6] [--dry-run]"""
import argparse, importlib.util
from serieslib import git, rewrite

ap = argparse.ArgumentParser()
ap.add_argument('fixer'); ap.add_argument('file'); ap.add_argument('first', type=int)
ap.add_argument('--branch', default='series/v6'); ap.add_argument('--dry-run', action='store_true')
a = ap.parse_args()
spec = importlib.util.spec_from_file_location('fixer', a.fixer)
fixer = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixer)
rewrite(a.branch, a.first, lambda c: {a.file: fixer.fix(git('show', f'{c}:{a.file}'))}, a.dry_run)
