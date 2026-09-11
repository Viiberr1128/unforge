#!/usr/bin/env python3
"""Refuse a public Unforge tree that looks like a person's workspace.

This check is generic on purpose. Maintainer-specific project names stay in a
private pre-push script that is not part of this repository.
"""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {'.git', 'node_modules', 'dist', 'artifacts', '.venv', '.venv-macos', '.tools', '__pycache__'}
HOME_PATH = re.compile(rb'/Users/(?!you(?:/|"|\'|$))[A-Za-z0-9._-]+')
PRIVATE_EMAIL = re.compile(
    rb'(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com|unforge\.(?:invalid|app|site)|github\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
)
KEY_BLOB = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
UNFORGE_HOME_DATA = re.compile(rb'\.local/share/unforge/[a-f0-9]{32}')
FORBIDDEN_NAMES = {
    '.env', 'credentials.json', 'auth.json', 'secrets.json', 'recovery-key',
    'recovery-key.txt', 'settings.json',
}


def tracked_files():
    result = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], capture_output=True, check=True)
    return [Path(name) for name in result.stdout.decode().split('\0') if name]


def main():
    failures = []
    if not (ROOT / '.git').exists():
        print('Public source check requires a Git checkout.', file=sys.stderr)
        return 1
    for relative in tracked_files():
        path = ROOT / relative
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.name.lower() in FORBIDDEN_NAMES or path.suffix.lower() in {'.sqlite', '.sqlite3', '.pem', '.key'}:
            failures.append(f'private filename {relative}')
            continue
        if not path.is_file():
            continue
        data = path.read_bytes()
        if HOME_PATH.search(data):
            failures.append(f'home path {relative}')
        if UNFORGE_HOME_DATA.search(data):
            failures.append(f'workspace project path {relative}')
        if KEY_BLOB.search(data):
            failures.append(f'private key {relative}')
        if PRIVATE_EMAIL.search(data) and relative.as_posix() not in {
            'THIRD_PARTY_NOTICES.md', 'docs/SECURITY.md', 'SECURITY.md',
        }:
            # Notices may cite upstream authors. Application source should not.
            if relative.suffix in {'.py', '.js', '.jsx', '.mjs', '.md', '.html', '.yml', '.yaml', '.json', '.sh'}:
                if relative.as_posix() != 'THIRD_PARTY_NOTICES.md':
                    failures.append(f'personal email {relative}')
    identity = subprocess.check_output(['git', '-C', str(ROOT), 'log', '-1', '--format=%an <%ae>'])
    if identity.strip() not in {b'Viiberr1128 <265590491+Viiberr1128@users.noreply.github.com>'}:
        # Only the public publisher identity is allowed on this mirror.
        failures.append('unapproved HEAD commit identity')
    if failures:
        print('Public source check failed. The tree still looks like a private workspace.', file=sys.stderr)
        for item in failures[:20]:
            print(item, file=sys.stderr)
        return 1
    print('Public source check passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
