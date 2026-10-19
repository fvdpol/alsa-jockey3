"""Shared helpers for the series fold tools: rewrite a series branch by changing
files in every commit from patch <first> onwards, keeping each commit's message,
author and dates. The setup commit S (the series base) is never touched."""
import os, subprocess, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
BASE = '475e468'           # setup commit S; the series is BASE..branch


def git(*a, inp=None, env=None):
    return subprocess.run(['git', '-C', REPO, *a], input=inp, capture_output=True,
                          text=True, check=True, env=env).stdout


def commits(branch):
    c = git('rev-list', '--reverse', f'{BASE}..{branch}').split()
    assert c, f'no commits in {BASE}..{branch}'
    return c


def message(c):
    """The commit message byte for byte; %B would add a trailing newline per rewrite."""
    raw = git('cat-file', 'commit', c)
    return raw[raw.index('\n\n') + 2:]


def rewrite(branch, first, new_files, dry, messages=None):
    """new_files(commit) -> {path: content or None}; None leaves the file as is.
    messages: {patch number: new commit message} for the patches to reword."""
    C = commits(branch)
    assert 1 <= first <= len(C), (first, len(C))
    parent = C[first - 2] if first > 1 else git('rev-parse', BASE).strip()
    idx = os.path.join(tempfile.gettempdir(), f'fold-idx-{os.getpid()}')
    for k in range(first - 1, len(C)):
        env = dict(os.environ, GIT_INDEX_FILE=idx)
        git('read-tree', C[k], env=env)
        for f, content in new_files(C[k]).items():
            if content is None:
                continue
            blob = git('hash-object', '-w', '--stdin', inp=content).strip()
            git('update-index', '--cacheinfo', f'100644,{blob},{f}', env=env)
        tree = git('write-tree', env=env).strip()
        os.unlink(idx)
        fmt = git('log', '-1', '--format=%an%x00%ae%x00%ad%x00%cn%x00%ce%x00%cd',
                  '--date=raw', C[k]).split('\0')
        cenv = dict(os.environ, GIT_AUTHOR_NAME=fmt[0], GIT_AUTHOR_EMAIL=fmt[1],
                    GIT_AUTHOR_DATE=fmt[2], GIT_COMMITTER_NAME=fmt[3],
                    GIT_COMMITTER_EMAIL=fmt[4], GIT_COMMITTER_DATE=fmt[5].strip())
        parent = git('commit-tree', tree, '-p', parent,
                     inp=(messages or {}).get(k + 1, message(C[k])), env=cenv).strip()
        print(f'p{k+1:02d} {C[k][:7]} -> {parent[:7]}')
    if not dry:
        git('update-ref', f'refs/heads/{branch}', parent, C[-1])
    print('tip', parent, '(dry run, branch not moved)' if dry else f'-> {branch}')
    return parent
