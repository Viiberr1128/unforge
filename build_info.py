"""Identify the running source or packaged build without an account or network."""
import json
import os
from functools import lru_cache
from pathlib import Path
import subprocess
import sys

CAPABILITIES = ['lanes', 'checks', 'releases', 'exchange', 'recovery', 'workspace-events', 'operating-view',
                'ai-outcome-plans', 'fleet-bridge-v2', 'ai-proposals']

@lru_cache(maxsize=1)
def current():
    root = Path(__file__).resolve().parent
    if getattr(sys, 'frozen', False):
        manifest = Path(sys.executable).resolve().parent.parent / 'BUILD.json'
        try:
            with manifest.open('rb') as source:
                data = json.loads(source.read(128 * 1024))
            if not isinstance(data, dict):
                raise ValueError('Invalid build identity')
            return {key: data.get(key) for key in ('version', 'sourceRevision', 'sourceDigest', 'sourceDirty', 'signing', 'notarized')} | {'capabilities': CAPABILITIES}
        except (OSError, ValueError, TypeError):
            return {'version': 'unknown', 'sourceRevision': None, 'capabilities': CAPABILITIES, 'metadataError': 'Build identity unavailable'}
    version = json.loads((root / 'package.json').read_text())['version']
    try:
        env = {key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
        top = subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], cwd=root, env=env, timeout=2).decode().strip()
        if Path(top).resolve() != root:
            raise ValueError('Source folder has no repository of its own')
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, env=env, timeout=2).decode().strip()
        dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, env=env, timeout=2))
    except (OSError, ValueError, subprocess.SubprocessError):
        revision, dirty = None, None
    return {'version': version, 'sourceRevision': revision, 'sourceDirty': dirty, 'signing': 'source', 'notarized': False, 'capabilities': CAPABILITIES}
