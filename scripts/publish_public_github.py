#!/usr/bin/env python3
"""Push the public Unforge source mirror after privacy checks.

This never copies ~/.local/share/unforge project workspaces. It only pushes
the current Git checkout of this repository. Set UNFORGE_PRIVATE_PUBLISH_CHECK
to a maintainer-only scanner that names private apps; that file must not live
in this repository.
"""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(args):
    return subprocess.run(args, cwd=ROOT, check=False)


def main():
    status = subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain']).decode()
    if status.strip():
        print('Save a version first. Uncommitted files are not published.', file=sys.stderr)
        return 1
    private = os.environ.get('UNFORGE_PRIVATE_PUBLISH_CHECK')
    if private:
        code = run([sys.executable, private]).returncode
        if code:
            return code
    code = run([sys.executable, str(ROOT / 'scripts' / 'check_public_source.py')]).returncode
    if code:
        return code
    remote = subprocess.check_output(['git', '-C', str(ROOT), 'remote', 'get-url', 'origin']).decode().strip()
    if remote not in {
        'https://github.com/Viiberr1128/unforge.git',
        'git@github.com:Viiberr1128/unforge.git',
    }:
        print('Origin is not the public Unforge mirror.', file=sys.stderr)
        return 1
    pushed = run(['git', 'push', 'origin', 'HEAD:main'])
    if pushed.returncode:
        return pushed.returncode
    print('Public Unforge source mirror updated.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
