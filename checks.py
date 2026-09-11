"""Check graph and cache. This is the Actions replacement: run on a lane or merge tree."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading

from engine import Problem
from lanes import atomic_json

MAX_OUTPUT = 128 * 1024
MAX_JOBS = 16


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Checks:
    def __init__(self, engine):
        self.engine = engine
        self.root = engine.home / '.check-receipts'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._busy = threading.Lock()

    def graph(self, pid, document=None):
        if document is not None:
            return self._validate(document)
        path = self.engine.root(pid) / '.unforge' / 'app.json'
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except (ValueError, OSError, UnicodeError):
                data = {}
            if isinstance(data, dict) and data.get('checks'):
                return self._validate(data['checks'])
        runtime = self.engine.root(pid) / '.unforge' / 'runtime.json'
        if runtime.is_file():
            try:
                config = json.loads(runtime.read_text(encoding='utf-8'))
            except (ValueError, OSError, UnicodeError):
                config = {}
            command = config.get('check') if isinstance(config, dict) else None
            if isinstance(command, list) and command:
                return self._validate({'schemaVersion': 1, 'jobs': [{'id': 'check', 'command': command}]})
        return self._validate({'schemaVersion': 1, 'jobs': [{'id': 'ok', 'command': ['python3', '-c', 'print("ok")']}]})

    def _validate(self, document):
        if not isinstance(document, dict) or document.get('schemaVersion') != 1:
            raise Problem('Check graph must use schemaVersion 1')
        jobs = document.get('jobs')
        if not isinstance(jobs, list) or not 1 <= len(jobs) <= MAX_JOBS:
            raise Problem('Choose 1–16 check jobs')
        result = {'schemaVersion': 1, 'jobs': []}
        for job in jobs:
            if not isinstance(job, dict) or not isinstance(job.get('id'), str) or not job['id'] or len(job['id']) > 40:
                raise Problem('Each check job needs a short id')
            command = job.get('command')
            if not isinstance(command, list) or not command or any(
                    not isinstance(arg, str) or not arg or len(arg) > 2000 or '\0' in arg for arg in command):
                raise Problem('Each check job needs a command argument list')
            timeout = job.get('timeoutSeconds', 300)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 900:
                raise Problem('Check timeouts must be between 1 and 900 seconds')
            cache = job.get('cache', True)
            if cache not in (True, False):
                raise Problem('cache must be true or false')
            result['jobs'].append({'id': job['id'], 'command': list(command), 'timeoutSeconds': int(timeout),
                                   'cache': cache})
        return result

    def _key(self, pid, tree, graph):
        return digest({'pid': pid, 'tree': tree, 'graph': graph})

    def receipt(self, pid, tree, graph=None):
        graph = graph or self.graph(pid)
        path = self.root / pid / (self._key(pid, tree, graph) + '.json')
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError, UnicodeError):
            return None
        return data if data.get('status') == 'passed' else None

    def passed(self, pid, tree):
        return self.receipt(pid, tree) is not None

    def run(self, pid, *, lane_id=None, cwd=None):
        if not self._busy.acquire(blocking=False):
            raise Problem('Another local check is running. Wait for it to finish.')
        try:
            with self.engine.lock:
                root = self.engine.root(pid)
                work = Path(cwd) if cwd else root
                if lane_id:
                    from lanes import Lanes
                    lane = Lanes(self.engine).get(pid, lane_id)
                    work = Path(lane['path'])
                    tree = lane['head']
                    if lane.get('dirty'):
                        raise Problem('Save the lane before running checks')
                else:
                    if self.engine.git(root, 'status', '--porcelain'):
                        raise Problem('Save a version before running checks on live source')
                    tree = self.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
                graph = self.graph(pid)
            cached = self.receipt(pid, tree, graph)
            if cached:
                return {**cached, 'reused': True}
            jobs = []
            env = {key: value for key, value in os.environ.items() if key in {'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'SYSTEMROOT', 'HOME'}}
            env.update(CI='1', NO_COLOR='1', MAKEFLAGS='-j2')
            for job in graph['jobs']:
                try:
                    completed = subprocess.run(
                        job['command'], cwd=work, env=env, capture_output=True, timeout=job['timeoutSeconds'])
                except FileNotFoundError as error:
                    raise Problem(f'Check {job["id"]} needs {job["command"][0]} on this Mac') from error
                except subprocess.TimeoutExpired as error:
                    raise Problem(f'Check {job["id"]} exceeded {job["timeoutSeconds"]} seconds') from error
                output = (completed.stdout + completed.stderr)[:MAX_OUTPUT].decode(errors='replace')
                jobs.append({'id': job['id'], 'exitCode': completed.returncode, 'output': output})
                if completed.returncode != 0:
                    receipt = dict(status='failed', tree=tree, graph=graph, jobs=jobs, createdAt=now(), reused=False)
                    atomic_json(self.root / pid / (self._key(pid, tree, graph) + '.json'), receipt)
                    return receipt
            receipt = dict(status='passed', tree=tree, graph=graph, jobs=jobs, createdAt=now(), reused=False)
            atomic_json(self.root / pid / (self._key(pid, tree, graph) + '.json'), receipt)
            return receipt
        finally:
            self._busy.release()
