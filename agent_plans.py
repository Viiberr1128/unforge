"""Read-only, base-bound plans for bounded local AI work."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import uuid

from engine import Problem

MAX_PLANS = 100
MAX_PLAN_BYTES = 128 * 1024
SOURCE_SUFFIXES = {
    '.c', '.cc', '.cpp', '.css', '.go', '.html', '.java', '.js', '.jsx', '.json',
    '.kt', '.md', '.mjs', '.php', '.py', '.rb', '.rs', '.sh', '.sql', '.swift',
    '.ts', '.tsx', '.vue', '.yaml', '.yml',
}
GENERIC_WORDS = {
    'add', 'and', 'build', 'change', 'fix', 'for', 'from', 'have', 'make', 'need',
    'project', 'should', 'that', 'the', 'this', 'update', 'want', 'with', 'work',
}


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical_digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


class AgentPlans:
    """Durable plan records. Planning never creates a lane or runs a provider."""

    def __init__(self, engine):
        self.engine = engine
        self._lock = threading.RLock()
        self._store = engine.home / '.agent-plans'
        self._store.mkdir(mode=0o700, exist_ok=True)
        info = self._store.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise Problem('Agent plans must be stored in a directory owned by this account.')

    def _path(self, plan_id):
        if not isinstance(plan_id, str) or not re.fullmatch(r'[a-f0-9]{32}', plan_id):
            raise Problem('Unknown agent plan.')
        return self._store / (plan_id + '.json')

    def _write(self, plan):
        record = dict(plan)
        record['digest'] = canonical_digest({key: value for key, value in record.items() if key != 'digest'})
        raw = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
        if len(raw) > MAX_PLAN_BYTES:
            raise Problem('Agent plan exceeds its storage limit.')
        descriptor, name = tempfile.mkstemp(prefix='.write-', dir=self._store)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path(record['id']))
            directory = os.open(self._store, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return record

    def _make_room(self):
        paths = list(self._store.glob('*.json'))
        if len(paths) < MAX_PLANS:
            return
        removable = []
        for path in paths:
            try:
                plan = self._read(path.stem)
            except Problem:
                continue
            if plan.get('status') in {'started', 'needs_input'}:
                removable.append((plan.get('createdAt', ''), path))
        for _, path in sorted(removable):
            path.unlink(missing_ok=True)
            if len(list(self._store.glob('*.json'))) < MAX_PLANS:
                return
        raise Problem('Saved agent plans are full. Start an older ready plan before making another.')

    def _read(self, plan_id):
        path = self._path(plan_id)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > MAX_PLAN_BYTES:
                    raise Problem('Saved agent plan is invalid.')
                plan = json.loads(source.read(MAX_PLAN_BYTES + 1))
        except FileNotFoundError as exc:
            raise Problem('Unknown agent plan.') from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise Problem('Saved agent plan is invalid.') from exc
        if not isinstance(plan, dict) or plan.get('schemaVersion') != 1 or plan.get('id') != plan_id:
            raise Problem('Saved agent plan is invalid.')
        digest = plan.get('digest')
        if digest != canonical_digest({key: value for key, value in plan.items() if key != 'digest'}):
            raise Problem('Saved agent plan checksum does not match.')
        return plan

    @staticmethod
    def _tokens(outcome):
        words = [word for word in dict.fromkeys(re.findall(r'[a-z0-9]+', outcome.lower()))
                 if len(word) >= 3 and word not in GENERIC_WORDS]
        return sorted(words, key=lambda word: -len(word))[:32]

    def _tracked_candidates(self, root, base, tokens):
        candidates = []
        raw = self.engine.git(root, 'ls-tree', '-r', '-l', '-z', base)
        for entry in (value for value in raw.split(b'\0') if value):
            header, separator, raw_path = entry.partition(b'\t')
            fields = header.split()
            if not separator or len(fields) != 4 or fields[1] != b'blob' or not fields[0].startswith(b'100'):
                continue
            try:
                size = int(fields[3])
                path = raw_path.decode('utf-8')
            except (ValueError, UnicodeDecodeError):
                continue
            if size > 128 * 1024:
                continue
            relative = Path(path)
            if any(part.lower() in {'.git', '.unforge', 'node_modules', 'dist', 'vendor'} for part in relative.parts):
                continue
            if relative.suffix.lower() not in SOURCE_SUFFIXES or path.endswith(('.min.js', '.map')):
                continue
            lowered = path.lower()
            path_score = sum(8 for token in tokens if token in lowered)
            candidates.append([path_score, path])
        by_path = {item[1]: item for item in candidates}
        # Git can search the exact saved tree without checking files out. This
        # prevents a relevant file late in a large tree from being missed by
        # the bounded content-reading pass below.
        for token in tokens[:12]:
            matches = self.engine.git_run(
                root, 'grep', '-I', '-l', '-z', '-i', '-F', '-e', token, base, '--',
                allowed_returncodes=(0, 1),
            )
            if matches.returncode:
                continue
            for raw in matches.stdout.split(b'\0'):
                if not raw:
                    continue
                _, separator, path = raw.decode('utf-8', errors='ignore').partition(':')
                if separator and path in by_path:
                    by_path[path][0] += 4
        return sorted((item for item in candidates if item[0] > 0), key=lambda item: (-item[0], len(item[1]), item[1]))

    def _focused_check(self, pid, base, selected):
        from checks import Checks
        root = self.engine.root(pid)
        raw_graph = self.engine.git_run(root, 'show', base + ':.unforge/app.json', allowed_returncodes=(0, 128))
        if raw_graph.returncode:
            return None
        graph = Checks(self.engine).graph(pid, tree=base)
        if not graph:
            return None
        stems = {Path(path).stem.lower().removesuffix('.test').removesuffix('.spec') for path in selected}
        usable = [job for job in graph['jobs'] if job['directory'] == '.' and len(job['command']) <= 32]
        if not usable:
            return None
        def score(job):
            haystack = (job['id'] + ' ' + ' '.join(job['command'])).lower()
            return sum(1 for stem in stems if len(stem) >= 3 and stem in haystack)
        ranked = sorted(usable, key=lambda job: (-score(job), job['id']))
        if score(ranked[0]) == 0:
            return None
        chosen = ranked[0]
        argv = list(chosen['command'])
        argv_sha = hashlib.sha256((json.dumps(argv, ensure_ascii=False, separators=(',', ':')) + '\n').encode()).hexdigest()
        return {
            'id': chosen['id'], 'argv': argv, 'timeoutSeconds': max(30, min(chosen['timeoutSeconds'], 900)),
            'argvSha256': argv_sha, 'graphPath': '.unforge/app.json',
            'graphSha256': hashlib.sha256(raw_graph.stdout).hexdigest(),
        }

    @staticmethod
    def _task_class(outcome):
        lowered = outcome.lower()
        if any(word in lowered for word in ('bug', 'broken', 'crash', 'fix', 'error')):
            return 'debugging'
        if any(word in lowered for word in ('test', 'coverage')):
            return 'tests'
        if any(word in lowered for word in ('copy', 'docs', 'documentation', 'readme')):
            return 'docs'
        if any(word in lowered for word in ('review', 'audit')):
            return 'review'
        return 'implementation'

    def create(self, pid, outcome):
        if not isinstance(outcome, str) or not outcome.strip() or len(outcome) > 8000:
            raise Problem('Outcome must contain 1–8000 characters.')
        outcome = outcome.strip()
        with self._lock, self.engine.lock:
            self._make_room()
            root = self.engine.root(pid)
            if self.engine.git(root, 'status', '--porcelain'):
                raise Problem('Save your current changes before planning new work.')
            base = self.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
            self.engine.validate_checkout_size(root, base)
            self.engine.validate_tree(root, base)
            tokens = self._tokens(outcome)
            ranked = self._tracked_candidates(root, base, tokens)
            selected = [path for _, path in ranked[:5]]
            if selected:
                selected_stems = {Path(path).stem.lower().removesuffix('.test').removesuffix('.spec') for path in selected}
                related = [path for _, path in ranked[5:] if ('test' in path.lower() or 'spec' in path.lower())
                           and any(len(stem) >= 3 and stem in path.lower() for stem in selected_stems)]
                selected = list(dict.fromkeys(selected + related[:max(0, 8 - len(selected))]))[:8]
            plan_id = uuid.uuid4().hex
            if not selected:
                plan = {
                    'schemaVersion': 1, 'id': plan_id, 'projectId': pid, 'baseVersion': base,
                    'createdAt': now(), 'status': 'needs_input', 'outcome': outcome,
                    'question': 'Which screen, feature, or exact file should this change start from?',
                    'provider': 'subscription-fleet', 'taskClass': self._task_class(outcome),
                    'size': 'small', 'acceptance': outcome, 'allowPaths': [],
                    'focusedCheck': None, 'projectContext': '',
                }
            else:
                task_class = self._task_class(outcome)
                plan = {
                    'schemaVersion': 1, 'id': plan_id, 'projectId': pid, 'baseVersion': base,
                    'createdAt': now(), 'status': 'ready', 'outcome': outcome,
                    'question': None, 'provider': 'subscription-fleet', 'taskClass': task_class,
                    'size': 'large' if len(selected) >= 7 else 'medium' if len(selected) >= 4 else 'small',
                    'acceptance': outcome,
                    'allowPaths': selected,
                    'focusedCheck': self._focused_check(pid, base, selected),
                    'projectContext': 'The approved source boundary is limited to: ' + ', '.join(selected),
                }
            return self._write(plan)

    def get(self, plan_id):
        with self._lock:
            return self._read(plan_id)

    def resolve(self, pid, plan_id, operation_id=None):
        with self._lock, self.engine.lock:
            plan = self._read(plan_id)
            if (plan['projectId'] == pid and plan['status'] == 'started' and
                    isinstance(operation_id, str) and plan.get('operationId') == operation_id):
                return plan
            if plan['projectId'] != pid or plan['status'] != 'ready':
                raise Problem('This agent plan is not ready for this project.')
            root = self.engine.root(pid)
            if self.engine.git(root, 'status', '--porcelain'):
                raise Problem('Save your current changes before starting this plan.')
            current = self.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
            if current != plan['baseVersion']:
                raise Problem('The project changed after this plan was made. Plan the work again.')
            return plan

    def claim(self, pid, plan_id, operation_id):
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 200:
            raise Problem('Starting a saved plan requires a stable operation ID.')
        with self._lock, self.engine.lock:
            plan = self._read(plan_id)
            if (plan['projectId'] == pid and plan.get('operationId') == operation_id and
                    plan['status'] in {'starting', 'started'}):
                return plan
            if plan['projectId'] != pid or plan['status'] != 'ready':
                raise Problem('This agent plan is not ready for this project.')
            root = self.engine.root(pid)
            if self.engine.git(root, 'status', '--porcelain'):
                raise Problem('Save your current changes before starting this plan.')
            current = self.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
            if current != plan['baseVersion']:
                raise Problem('The project changed after this plan was made. Plan the work again.')
            plan.update(status='starting', claimedAt=now(), operationId=operation_id)
            return self._write(plan)

    def release_claim(self, pid, plan_id, operation_id):
        with self._lock:
            plan = self._read(plan_id)
            if (plan['projectId'] == pid and plan['status'] == 'starting' and
                    plan.get('operationId') == operation_id):
                plan['status'] = 'ready'
                for key in ('claimedAt', 'operationId'):
                    plan.pop(key, None)
                return self._write(plan)
            return plan

    def mark_started(self, pid, plan_id, job_id, operation_id):
        with self._lock:
            plan = self._read(plan_id)
            if (plan['projectId'] == pid and plan['status'] == 'started' and
                    plan.get('jobId') == job_id and plan.get('operationId') == operation_id):
                return plan
            if plan['projectId'] != pid or plan['status'] not in {'ready', 'starting'}:
                raise Problem('This agent plan is no longer ready to start.')
            if not isinstance(job_id, str) or not re.fullmatch(r'[a-f0-9]{32}', job_id):
                raise Problem('Agent job identity is invalid.')
            if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 200 or (
                    plan['status'] == 'starting' and plan.get('operationId') != operation_id):
                raise Problem('Agent operation identity is invalid.')
            plan.update(status='started', startedAt=now(), jobId=job_id, operationId=operation_id)
            return self._write(plan)
