#!/usr/bin/env python3
"""Verify a built release in isolation using Python and Git, without Node or AI calls."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def extract_verified(archive, destination):
    names = set()
    roots = set()
    total = 0
    with tarfile.open(archive, 'r:gz') as package:
        for member in package:
            path = PurePosixPath(member.name)
            require(not path.is_absolute() and '..' not in path.parts and len(path.parts) >= 2,
                    'Unsafe archive path: ' + member.name)
            require(member.isfile(), 'Only regular files are allowed: ' + member.name)
            require(member.name not in names, 'Duplicate archive entry: ' + member.name)
            total += member.size
            require(total <= 256 * 1024 * 1024 and len(names) < 20000, 'Archive exceeds smoke extraction limits')
            names.add(member.name)
            roots.add(path.parts[0])
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.extractfile(member) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o777)
    require(len(roots) == 1, 'Archive must contain one release directory')
    root = destination / next(iter(roots))
    manifest_path = root / 'MANIFEST.sha256.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    require(isinstance(manifest, dict) and bool(manifest), 'Missing manifest entries')
    actual = {str(path.relative_to(root)) for path in root.rglob('*') if path.is_file()}
    require(actual == set(manifest) | {'MANIFEST.sha256.json'}, 'Manifest does not cover exact archive contents')
    for relative, expected in manifest.items():
        path = PurePosixPath(relative)
        require(not path.is_absolute() and '..' not in path.parts, 'Unsafe manifest path')
        require(hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected,
                'Manifest digest mismatch: ' + relative)
    return root, len(manifest)


def stop_group(process):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    # A parent may exit before its descendants. Reap the complete owned group.
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return
    os.killpg(process.pid, signal.SIGKILL)
    for _ in range(20):
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise RuntimeError('Release server descendants remain after shutdown')


def smoke(archive):
    process = None
    with tempfile.TemporaryDirectory(prefix='unforge-release-smoke-') as directory:
        base = Path(directory)
        root, entries = extract_verified(archive, base / 'extracted')
        marker = base / 'unexpected-node-call'
        bin_dir = base / 'bin'
        bin_dir.mkdir()
        for command in ('node', 'npm', 'npx'):
            stub = bin_dir / command
            stub.write_text('#!/bin/sh\nprintf called > "$UNFORGE_SMOKE_NODE_MARKER"\nexit 97\n', encoding='utf-8')
            stub.chmod(0o755)
        env = os.environ.copy()
        env.update(PATH=str(bin_dir) + os.pathsep + env.get('PATH', ''),
                   UNFORGE_SMOKE_NODE_MARKER=str(marker), PYTHONDONTWRITEBYTECODE='1')
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        log_path = base / 'server.log'
        with log_path.open('wb') as log:
            process = subprocess.Popen([str(root / 'start.sh'), '--home', str(base / 'data'), '--port', str(port)],
                                       cwd=root, env=env, stdout=log, stderr=log, start_new_session=True)
            try:
                def get(path):
                    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                    try:
                        connection.request('GET', path)
                        response = connection.getresponse()
                        return response.status, response.read()
                    finally:
                        connection.close()
                deadline = time.monotonic() + 15
                while True:
                    require(process.poll() is None, 'Release server exited: ' + log_path.read_text(encoding='utf-8', errors='replace'))
                    try:
                        status, body = get('/api/health')
                        if status == 200:
                            require(json.loads(body)['ok'] is True, 'Unhealthy server')
                            break
                    except OSError:
                        pass
                    require(time.monotonic() < deadline, 'Server did not become ready')
                    time.sleep(0.1)
                status, ui = get('/')
                require(status == 200 and b'<html' in ui.lower(), 'Built UI not served')
                assets = re.findall(rb'(?:src|href)="(/assets/[^" ]+)"', ui)
                require(bool(assets), 'Built UI has no bundled asset links')
                for asset in assets:
                    require(get(asset.decode())[0] == 200, 'Built asset missing')

                def cli(*args):
                    result = subprocess.run([sys.executable, str(root / 'unforge.py'), '--port', str(port), *args],
                                            cwd=root, env=env, capture_output=True, text=True, timeout=45)
                    require(result.returncode == 0, 'CLI failed: ' + result.stderr)
                    return json.loads(result.stdout)
                project = cli('create', 'Release smoke', '--description', 'Independent artifact verification')
                pid = project['id']
                first = project['history'][0]['id']
                text = base / 'edit.txt'
                text.write_text('# Verified release\n', encoding='utf-8')
                edited = cli('write', pid, 'README.md', '--from-file', str(text))
                require(edited['dirty'], 'Write did not update working files')
                saved = cli('save', pid, 'Verify packaged write')
                require(not saved['dirty'], 'Save left dirty worktree')
                restored = cli('restore', pid, first)
                require(len(restored['history']) == 3, 'Restore failed to retain history')
                bundle = base / 'roundtrip.bundle'
                cli('export', pid, '--output', str(bundle))
                imported = cli('import', str(bundle))
                require(imported['id'] != pid and imported['history'] == restored['history'], 'Import did not preserve independent history')
                clone = base / 'ordinary-git-clone'
                git_env = {key: value for key, value in env.items() if not key.startswith('GIT_')}
                git_env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
                result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', 'clone', '--template=', str(bundle), str(clone)],
                                        env=git_env, capture_output=True, text=True, timeout=30)
                require(result.returncode == 0, 'Ordinary Git recovery failed: ' + result.stderr)
                require((clone / 'README.md').read_text(encoding='utf-8') == '# Release smoke\n\nIndependent artifact verification\n', 'Recovered content differs')
                require(not marker.exists(), 'Built startup invoked Node/npm/npx')
            finally:
                stop_group(process)
                process = None
        return {'archive': str(archive), 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                'manifestFiles': entries, 'checks': ['manifest', 'built-start-no-node', 'health', 'ui-assets',
                'create', 'edit', 'save', 'restore', 'export', 'import', 'ordinary-git-recovery', 'process-group-cleanup']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(smoke(args.archive.resolve()), indent=2))
        return 0
    except Exception as error:
        print('Release smoke failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
