"""Local checks of an immutable source/configuration pair, with bounded execution."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import tempfile
import threading
import time

from engine import Problem
from lanes import atomic_json
from runtime import heavy_lock, terminate_process_group

MAX_OUTPUT = 128 * 1024
MAX_JOBS = 16
MAX_ARTIFACT = 128 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def resolve_commit(engine, root, revision):
    if not isinstance(revision, str) or not revision or revision.startswith('-') or len(revision) > 240:
        raise Problem('Choose a saved source revision')
    return engine.git(root, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()


def materialize_commit(engine, root, revision, target):
    """No shared index, refs, config, hooks, alternates, or working files."""
    engine.validate_checkout_size(root, revision)
    engine.validate_tree(root, revision)
    target.mkdir(parents=True)
    engine.git(target, 'init', '--template=', '-b', 'unforge-candidate')
    engine.git(target, 'fetch', '--depth=1', '--no-tags', str(root), revision)
    engine.git(target, 'checkout', '--detach', revision)
    return target


def artifact_manifest(folder):
    """Bound artifacts and reject links/private files before copying or serving."""
    if folder.is_symlink() or not folder.is_dir():
        raise Problem('Build the configured artifact directory before publishing')
    files, size = [], 0
    for path in sorted(folder.rglob('*')):
        relative = path.relative_to(folder)
        if any(part in ('.git', '.unforge', 'node_modules', '.app-data') for part in relative.parts):
            continue
        if path.is_symlink():
            raise Problem('Release artifacts cannot contain symbolic links')
        if path.is_dir():
            continue
        if not path.is_file() or any(part.lower().startswith('.env') for part in relative.parts):
            raise Problem('Release artifacts must contain ordinary non-private files')
        size += path.stat().st_size
        if size > MAX_ARTIFACT or len(files) >= 10000:
            raise Problem('Release artifact exceeds 128 MiB or 10,000 files')
        checksum = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(65536), b''):
                checksum.update(chunk)
        files.append({'path': relative.as_posix(), 'sha256': checksum.hexdigest(), 'bytes': path.stat().st_size})
    if not files:
        raise Problem('The release artifact is empty')
    return files


def copy_artifact(source, target):
    manifest = artifact_manifest(source)
    target.mkdir(parents=True, exist_ok=True)
    for item in manifest:
        dest = target / item['path']
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / item['path'], dest)
    if artifact_manifest(target) != manifest:
        raise Problem('Artifact changed while it was being captured')
    return digest(manifest)


def prune_static_artifacts(engine, pid):
    """Keep latest check output and the current/previous published artifacts.

    Only these content-addressed generated stores are touched. Source history,
    backup archives, arbitrary folders, and receipt history remain unchanged.
    """
    keep = set()
    receipt_root = engine.home / '.check-receipts' / pid
    latest = None
    for path in receipt_root.glob('*.json'):
        try:
            item = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(item, dict) and item.get('status') == 'passed' and (latest is None or item.get('createdAt', '') > latest.get('createdAt', '')):
            latest = item
    for artifact in (latest or {}).get('artifacts', {}).values():
        keep.add(str(Path(artifact['path']).resolve()))
    try:
        records = json.loads((engine.home / '.releases' / (pid + '.json')).read_text())
    except (OSError, ValueError):
        records = []
    if not isinstance(records, list):
        records = []
    seen = {}
    for item in reversed(records):
        if not isinstance(item, dict) or not item.get('observed') or item.get('operation') != 'publish' or not item.get('artifactPath'):
            continue
        retained = seen.setdefault(item.get('destinationId'), set())
        artifact = str(Path(item['artifactPath']).resolve())
        if artifact in retained or len(retained) < 2:
            retained.add(artifact)
            keep.add(artifact)
    for store in (engine.home / '.check-artifacts' / pid, engine.home / '.releases' / pid / 'artifacts'):
        if not store.is_dir() or store.is_symlink() or not store.resolve().is_relative_to(engine.home):
            continue
        for path in store.iterdir():
            if re.fullmatch('[0-9a-f]{64}', path.name) and path.is_dir() and not path.is_symlink() and str(path.resolve()) not in keep:
                shutil.rmtree(path)


def run_bounded(command, *, cwd, env, timeout, limit=MAX_OUTPUT):
    """Drain output continuously and stop the complete owned process group."""
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    output = bytearray()
    truncated = False
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    stopped = False
    try:
        while True:
            for key, _ in selector.select(.05):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    remaining = max(0, limit - len(output))
                    output.extend(chunk[:remaining])
                    truncated |= len(chunk) > remaining
            code = process.poll()
            if code is not None and not selector.get_map():
                return {'exitCode': code, 'output': output.decode(errors='replace'), 'outputTruncated': truncated}
            if time.monotonic() >= deadline:
                return {'exitCode': None, 'output': output.decode(errors='replace'),
                        'outputTruncated': truncated, 'timedOut': True}
            if code is not None and not stopped:
                # Close descendant writers, then drain the remaining buffered output.
                terminate_process_group(process)
                stopped = True
    finally:
        selector.close()
        terminate_process_group(process)
        process.stdout.close()


class Checks:
    def __init__(self, engine):
        self.engine = engine
        self.root = engine.home / '.check-receipts'
        self.artifacts = engine.home / '.check-artifacts'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._busy = threading.Lock()

    def _document(self, root, tree, path):
        size = self.engine.git_run(root, 'cat-file', '-s', tree + ':' + path, allowed_returncodes=(0, 128))
        if size.returncode:
            return None
        if int(size.stdout) > MAX_OUTPUT:
            raise Problem(f'{path} settings exceed 128 KiB')
        raw = self.engine.git_run(root, 'show', tree + ':' + path, allowed_returncodes=(0, 128))
        if raw.returncode:
            return None
        try:
            value = json.loads(raw.stdout.decode('utf-8'))
        except (ValueError, UnicodeError) as error:
            raise Problem(f'{path} settings are damaged') from error
        if not isinstance(value, dict):
            raise Problem(f'{path} settings must be an object')
        return value

    def graph(self, pid, document=None, *, tree=None):
        if document is not None:
            return self._validate(document)
        root = self.engine.root(pid)
        tree = resolve_commit(self.engine, root, tree or 'HEAD')
        app = self._document(root, tree, '.unforge/app.json') or {}
        if app.get('checks') is not None:
            return self._validate(app['checks'])
        config = self._document(root, tree, '.unforge/runtime.json') or {}
        if config.get('check'):
            jobs = []
            for name in ('prepare', 'check'):
                if config.get(name):
                    jobs.append({'id': name, 'command': config[name], 'directory': config.get('directory', '.'),
                                 'timeoutSeconds': config.get('checkTimeoutSeconds', 300)})
            return self._validate({'schemaVersion': 1, 'jobs': jobs})
        return None

    def _validate(self, document):
        if not isinstance(document, dict) or document.get('schemaVersion') != 1:
            raise Problem('Check graph must use schemaVersion 1')
        jobs = document.get('jobs')
        if not isinstance(jobs, list) or not 1 <= len(jobs) <= MAX_JOBS:
            raise Problem('Choose 1–16 check jobs')
        result, seen = {'schemaVersion': 1, 'jobs': []}, set()
        for job in jobs:
            if not isinstance(job, dict) or not isinstance(job.get('id'), str) or not job['id'] or len(job['id']) > 40 or job['id'] in seen:
                raise Problem('Each check job needs a unique short id')
            seen.add(job['id'])
            command = job.get('command')
            if not isinstance(command, list) or not command or any(
                    not isinstance(arg, str) or not arg or len(arg) > 2000 or '\0' in arg for arg in command):
                raise Problem('Each check job needs a command argument list')
            timeout = job.get('timeoutSeconds', 300)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 900:
                raise Problem('Check timeouts must be between 1 and 900 seconds')
            cache = job.get('cache', True)
            if not isinstance(cache, bool):
                raise Problem('cache must be true or false')
            directory = job.get('directory', '.')
            if not isinstance(directory, str) or not directory or Path(directory).is_absolute() or '..' in Path(directory).parts or '\\' in directory:
                raise Problem('Check directory must stay inside the source')
            result['jobs'].append({'id': job['id'], 'command': list(command), 'timeoutSeconds': int(timeout), 'cache': cache, 'directory': directory})
        return result

    def _key(self, pid, tree, graph):
        return digest({'pid': pid, 'tree': tree, 'graph': graph, 'executionVersion': 2})

    def result(self, pid, tree, graph=None):
        root = self.engine.root(pid)
        tree = resolve_commit(self.engine, root, tree)
        graph = graph if graph is not None else self.graph(pid, tree=tree)
        if graph is None:
            return None
        path = self.root / pid / (self._key(pid, tree, graph) + '.json')
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError, UnicodeError):
            return None
        return data if isinstance(data, dict) and data.get('tree') == tree and data.get('graph') == graph else None

    def receipt(self, pid, tree, graph=None):
        data = self.result(pid, tree, graph)
        return data if data and data.get('status') == 'passed' else None

    def passed(self, pid, tree):
        graph = self.graph(pid, tree=tree)
        return bool(graph and all(job['cache'] for job in graph['jobs']) and self.receipt(pid, tree, graph))

    def _capture_artifacts(self, pid, root, tree, work, previous):
        document = self._document(root, tree, '.unforge/app.json') or {}
        artifacts = dict((previous or {}).get('artifacts') or {})
        for dest in document.get('destinations') or []:
            if not isinstance(dest, dict) or dest.get('type') not in ('local', 'cloudflare-pages'):
                continue
            directory = dest.get('directory') or ('dist' if dest.get('type') == 'cloudflare-pages' else '.')
            if directory == '.':
                continue
            if not isinstance(directory, str) or Path(directory).is_absolute() or '..' in Path(directory).parts:
                raise Problem('Artifact directory must stay inside the source')
            source = work / directory
            if not source.is_dir():
                continue
            checksum = digest(artifact_manifest(source))
            target = self.artifacts / pid / checksum
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix='.artifact-', dir=target.parent) as temp:
                    stage = Path(temp) / 'contents'
                    copy_artifact(source, stage)
                    os.rename(stage, target)
            artifacts[directory] = {'path': str(target), 'digest': checksum}
        return artifacts

    def run(self, pid, *, lane_id=None, cwd=None, tree=None):
        if not self._busy.acquire(blocking=False):
            raise Problem('Another local check is running. Wait for it to finish.')
        lease = None
        try:
            lease = heavy_lock()
            with self.engine.lock:
                root = self.engine.root(pid)
                selected = Path(cwd) if cwd else root
                if lane_id:
                    from lanes import Lanes
                    selected = Path(Lanes(self.engine).get(pid, lane_id)['path'])
                if tree is None and self.engine.git(selected, 'status', '--porcelain'):
                    raise Problem('Save the source before running checks')
                tree = resolve_commit(self.engine, selected, tree or 'HEAD')
                graph = self.graph(pid, tree=tree)
                if graph is None:
                    return dict(status='not-configured', tree=tree, graph=None, jobs=[], createdAt=now(), reused=False)
            cached = self.receipt(pid, tree, graph)
            if cached and any(not Path(item['path']).is_dir() for item in cached.get('artifacts', {}).values()):
                cached = None  # Rebuild derived outputs reclaimed by bounded retention.
            if cached and all(job['cache'] for job in graph['jobs']):
                return {**cached, 'reused': True}
            with tempfile.TemporaryDirectory(prefix='unforge-check-') as temporary:
                work = materialize_commit(self.engine, root, tree, Path(temporary) / 'source')
                jobs = []
                env = {key: value for key, value in os.environ.items() if key in {'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'SYSTEMROOT', 'HOME'}}
                env.update(CI='1', NO_COLOR='1', MAKEFLAGS='-j2', CMAKE_BUILD_PARALLEL_LEVEL='2')
                for job in graph['jobs']:
                    # Sequential jobs may prepare dependencies for each other. Without
                    # declared outputs, a fresh graph must rerun those preparations too.
                    try:
                        directory = (work / job['directory']).resolve()
                        if not directory.is_relative_to(work.resolve()) or not directory.is_dir():
                            raise Problem('Check directory is missing or outside the source')
                        completed = run_bounded(job['command'], cwd=directory, env=env, timeout=job['timeoutSeconds'])
                    except FileNotFoundError as error:
                        raise Problem(f'Check {job["id"]} needs {job["command"][0]} on this Mac') from error
                    jobs.append({'id': job['id'], **completed, 'reused': False})
                    if completed.get('timedOut') or completed['exitCode'] != 0:
                        receipt = dict(status='failed', tree=tree, graph=graph, jobs=jobs, createdAt=now(), reused=False)
                        atomic_json(self.root / pid / (self._key(pid, tree, graph) + '.json'), receipt)
                        return receipt
                if self.engine.git(work, 'rev-parse', 'HEAD').decode().strip() != tree or self.engine.git(work, 'diff', '--name-only', 'HEAD'):
                    receipt = dict(status='failed', tree=tree, graph=graph, jobs=jobs, createdAt=now(), reused=False,
                                   error='Checks changed saved source. Update the source and check that version instead.')
                    atomic_json(self.root / pid / (self._key(pid, tree, graph) + '.json'), receipt)
                    return receipt
                receipt = dict(status='passed', tree=tree, graph=graph, jobs=jobs, createdAt=now(), reused=False,
                               artifacts=self._capture_artifacts(pid, root, tree, work, cached))
                atomic_json(self.root / pid / (self._key(pid, tree, graph) + '.json'), receipt)
                prune_static_artifacts(self.engine, pid)
                return receipt
        finally:
            if lease is not None:
                os.close(lease)
            self._busy.release()
