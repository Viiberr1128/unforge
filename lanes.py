"""Durable Git worktrees so people and agents can commit without sharing one checkout."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from engine import MAX_TEXT, Problem, UNSET
from recovery_links import MAX_METADATA, MAX_RECORDS, validate_lane_repository

BRANCH_PREFIX = 'unforge-lane-'
MAX_LANES = 32
MAX_NAME = 80
MAX_CLAIMS = 64
MAX_CLAIM_LENGTH = 240
ACTIVE_STATUSES = ('open', 'needs_restack', 'conflict')


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_claimed_paths(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_CLAIMS:
        raise Problem('Claim at most 64 project-relative files or folders')
    result = []
    for raw in value:
        if not isinstance(raw, str) or not raw or raw != raw.strip() or len(raw) > MAX_CLAIM_LENGTH:
            raise Problem('Each claimed path must be a project-relative path no longer than 240 characters')
        if ('\\' in raw or raw.startswith('/') or '//' in raw or
                any(ord(character) < 32 or ord(character) == 127 for character in raw) or
                any(character in raw for character in '*?[]{}')):
            raise Problem('Claimed paths cannot be absolute, contain globs, backslashes, or control characters')
        body = raw[:-1] if raw.endswith('/') else raw
        parts = body.split('/')
        if not body or not parts or any(part in ('', '.', '..') for part in parts):
            raise Problem('Claimed paths must stay inside the project; use a trailing / for a folder')
        if raw in result:
            raise Problem('Claimed paths must be unique')
        result.append(raw)
    return result


def claim_matches(claim, path):
    return path.startswith(claim) if claim.endswith('/') else path == claim


def claims_overlap(left, right):
    left_prefix, right_prefix = left.endswith('/'), right.endswith('/')
    if left_prefix and right_prefix:
        return left.startswith(right) or right.startswith(left)
    if left_prefix:
        return right.startswith(left)
    if right_prefix:
        return left.startswith(right)
    return left == right


class Lanes:
    def __init__(self, engine):
        self.engine = engine
        self.root = engine.home / '.lanes'
        if self.root.is_symlink() or self.root.exists() and not self.root.is_dir():
            raise Problem('Lane storage must be a private workspace directory.')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _index_path(self, pid):
        if not isinstance(pid, str) or not re.fullmatch(r'[a-f0-9]{32}', pid):
            raise Problem('Unknown project')
        path = self.root / (pid + '.json')
        if self.root.is_symlink() or path.is_symlink():
            raise Problem('Lane storage cannot follow symbolic links.')
        return path

    def _work(self, pid, lid):
        if (not isinstance(pid, str) or not re.fullmatch(r'[a-f0-9]{32}', pid)
                or not isinstance(lid, str) or not re.fullmatch(r'[a-f0-9]{32}', lid)):
            raise Problem('Invalid managed lane identity.')
        path = self.root / pid / lid
        if any(folder.is_symlink() for folder in (self.root, self.root / pid, path)):
            raise Problem('Lane storage cannot follow symbolic links.')
        return path

    def _load(self, pid):
        path = self._index_path(pid)
        if not path.is_file():
            return []
        if path.is_symlink() or path.stat().st_size > MAX_METADATA:
            raise Problem('Lane index is not an ordinary bounded file.')
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, list) or len(data) > MAX_RECORDS:
            raise Problem('Lane index is damaged. Existing worktrees have been left untouched.')
        seen = set()
        for item in data:
            lid = item.get('id') if isinstance(item, dict) else None
            if not isinstance(lid, str) or not re.fullmatch(r'[a-f0-9]{32}', lid) or lid in seen:
                raise Problem('Lane index contains an invalid or duplicated identity.')
            seen.add(lid)
            # Captured absolute paths are historical metadata, never authority.
            item['path'] = str(self._work(pid, lid))
        return data

    def _save(self, pid, records):
        atomic_json(self._index_path(pid), records)

    def _record(self, pid, lid):
        if not isinstance(lid, str) or not re.fullmatch(r'[a-f0-9]{32}', lid):
            raise Problem('Unknown lane')
        for item in self._load(pid):
            if item.get('id') == lid:
                self._validate_work(pid, item)
                return item
        raise Problem('Unknown lane')

    def _replace(self, pid, record):
        records = [record if item.get('id') == record['id'] else item for item in self._load(pid)]
        if not any(item.get('id') == record['id'] for item in records):
            records.append(record)
        self._save(pid, records)
        return record

    def _head(self, repo):
        return self.engine.git(repo, 'rev-parse', 'HEAD').decode().strip()

    def _validate_work(self, pid, record):
        work = self._work(pid, record['id'])
        if work.exists() or record.get('status') in ('open', 'needs_restack', 'conflict'):
            admin = validate_lane_repository(self.engine.home, pid, record['id'])
            actual = self.engine.git(work, 'rev-parse', '--path-format=absolute', '--show-toplevel',
                                     '--git-common-dir', '--absolute-git-dir').decode().splitlines()
            if (len(actual) != 3 or Path(actual[0]) != work
                    or Path(actual[1]) != self.engine.root(pid) / '.git' or Path(actual[2]) != admin):
                raise Problem('Lane Git configuration leaves its registered workspace. Existing work was preserved.')

    def _refresh(self, pid, record):
        self._validate_work(pid, record)
        work = self._work(pid, record['id'])
        if work.is_dir() and (work / '.git').exists() or work.is_dir() and (work / '.git').is_file():
            try:
                record['head'] = self._head(work)
                record['dirty'] = bool(self.engine.git(work, 'status', '--porcelain'))
                record['changedFiles'] = self.changed_files(pid, record['id'])
            except Problem:
                record['dirty'] = True
        return record

    @staticmethod
    def _claim_conflicts(record, records):
        claims = record.get('claimedPaths') or []
        conflicts = []
        if not claims or record.get('status') not in ACTIVE_STATUSES:
            return conflicts
        for other in records:
            if other.get('id') == record.get('id') or other.get('status') not in ACTIVE_STATUSES:
                continue
            overlaps = [
                {'claimedPath': mine, 'otherClaimedPath': theirs}
                for mine in claims for theirs in (other.get('claimedPaths') or [])
                if claims_overlap(mine, theirs)
            ]
            if overlaps:
                conflicts.append({'laneId': other.get('id'), 'laneName': other.get('name'), 'overlaps': overlaps})
        return conflicts

    def _public(self, pid, record, records=None):
        item = self._refresh(pid, dict(record))
        result = {key: item.get(key) for key in (
            'id', 'name', 'status', 'parent', 'base', 'head', 'branch', 'path',
            'claimedPaths', 'changedFiles', 'dirty', 'createdAt', 'updatedAt', 'conflictFiles', 'baseRef',
            'incomingTitle', 'incomingNote')}
        result['claimConflicts'] = self._claim_conflicts(item, records if records is not None else self._load(pid))
        return result

    def list(self, pid):
        self.engine.root(pid)
        records = self._load(pid)
        return {'lanes': [self._public(pid, item, records) for item in records]}

    def get(self, pid, lid):
        self.engine.root(pid)
        return self._public(pid, self._record(pid, lid))

    def create(self, pid, name, parent=None, claimed_paths=None, base_ref=None):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME:
            raise Problem('Lane name must contain 1–80 characters')
        if parent and base_ref:
            raise Problem('Choose either a parent lane or a production ref, not both')
        if base_ref is not None and (not isinstance(base_ref, str) or not base_ref.startswith('refs/') or not base_ref.endswith('/production')):
            raise Problem('Choose an existing full refs/.../production ref')
        paths = validate_claimed_paths(claimed_paths)
        with self.engine.lock:
            root = self.engine.root(pid)
            if base_ref is None and self.engine.git(root, 'status', '--porcelain'):
                raise Problem('Save your current changes before opening a lane')
            records = self._load(pid)
            if len([item for item in records if item.get('status') in ACTIVE_STATUSES]) >= MAX_LANES:
                raise Problem('This project already has 32 active lanes. Merge or close one first.')
            live = self._head(root)
            parent_id = None
            base = live
            needs_identity = False
            if base_ref is not None:
                self.engine.git(root, 'check-ref-format', base_ref)
                base = self.engine.git(root, 'show-ref', '--verify', '--hash', base_ref).decode().strip()
                base = self.engine.git(root, 'rev-parse', base + '^{commit}').decode().strip()
                self.engine.validate_checkout_size(root, base)
                needs_identity = self.engine.validate_tree(root, base, require_metadata=False) is None
            if parent:
                parent_record = self._record(pid, parent)
                parent_id = parent_record['id']
                if parent_record.get('status') == 'merged':
                    base = live
                else:
                    parent_work = Path(parent_record['path'])
                    if self.engine.git(parent_work, 'status', '--porcelain'):
                        raise Problem('Save the parent lane before opening a child')
                    base = self._head(parent_work)
            lid = uuid.uuid4().hex
            work = self._work(pid, lid)
            if work.exists():
                raise Problem('Lane workspace already exists')
            work.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            branch = BRANCH_PREFIX + lid
            try:
                self.engine.git(root, 'worktree', 'add', '-b', branch, str(work), base)
                if needs_identity:
                    marker = self.engine.safe_path(work, '.unforge/project.json')
                    self._write_text(marker, json.dumps(self.engine.metadata(root)) + '\n')
                    self.engine.git(work, 'add', '-f', '--', '.unforge/project.json')
                    self.engine.git(work, 'commit', '-m', 'Record local project identity for production lane')
            except Exception:
                if work.exists():
                    self.engine.git(root, 'worktree', 'remove', '--force', str(work), allowed_returncodes=(0, 128))
                    shutil.rmtree(work, ignore_errors=True)
                self.engine.git(root, 'update-ref', '-d', 'refs/heads/' + branch, allowed_returncodes=(0, 1))
                raise
            record = dict(id=lid, name=name.strip(), status='open', parent=parent_id, base=base,
                          head=self._head(work), branch=branch, path=str(work), claimedPaths=list(paths), baseRef=base_ref,
                          changedFiles=[], dirty=False, createdAt=now(), updatedAt=now(), conflictFiles=[])
            self._replace(pid, record)
            return self._public(pid, record)

    def write(self, pid, lid, path, content, expected_content=UNSET):
        if isinstance(path, str) and '/.unforge/project.json' in '/' + path.lower():
            raise Problem('Project metadata is managed by Unforge')
        if not isinstance(content, str) or len(content.encode()) > MAX_TEXT or '\0' in content:
            raise Problem('File must be text smaller than 128 KiB')
        with self.engine.lock:
            self.engine.root(pid)
            record = self._record(pid, lid)
            if record.get('status') not in ('open', 'needs_restack', 'conflict'):
                raise Problem('This lane can no longer be edited')
            work = Path(record['path'])
            target = self.engine.safe_path(work, path)
            if self.engine.git(work, 'check-ignore', '--', path, allowed_returncodes=(0, 1)):
                raise Problem('This file is excluded from saved versions by a Git ignore rule')
            if expected_content is not UNSET:
                if expected_content is None:
                    if target.exists():
                        raise Problem('This file already exists. Reload it before saving.')
                elif isinstance(expected_content, str):
                    if not target.is_file() or target.read_text(encoding='utf-8') != expected_content:
                        raise Problem('This file changed outside your editor. Reload it before saving.')
                else:
                    raise Problem('expectedContent must be text or null for a new file')
            self._write_text(target, content)
            record['updatedAt'] = now()
            self._replace(pid, record)
            return self.get(pid, lid)

    @staticmethod
    def _write_text(target, content):
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.unforge-write-', dir=target.parent)
        try:
            if target.exists():
                os.fchmod(descriptor, target.stat().st_mode & 0o777)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def save(self, pid, lid, message):
        if not isinstance(message, str) or not message.strip() or len(message) > 500:
            raise Problem('Version description must contain 1–500 characters')
        with self.engine.lock:
            record = self._record(pid, lid)
            if record.get('status') not in ('open', 'needs_restack', 'conflict'):
                raise Problem('This lane can no longer be saved')
            work = Path(record['path'])
            if self.engine.git(work, 'rev-parse', '--verify', 'MERGE_HEAD', allowed_returncodes=(0, 128)):
                raise Problem('Resolve every restack conflict before saving the lane')
            paths = self.engine.git(work, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').decode().split('\0')
            for path in filter(None, paths):
                self.engine.safe_path(work, path)
            self.engine.git(work, 'add', '-A', '--', '.')
            if self.engine.git(work, 'diff', '--cached', '--name-only'):
                self.engine.git(work, 'commit', '-m', message.strip())
            record['head'] = self._head(work)
            record['updatedAt'] = now()
            if record.get('status') == 'conflict' and not self.engine.git(work, 'status', '--porcelain'):
                record['status'] = 'open'
                record['conflictFiles'] = []
            self._replace(pid, record)
            return self.get(pid, lid)

    def changed_files(self, pid, lid):
        record = self._record(pid, lid)
        work = Path(record['path'])
        if not work.is_dir():
            return list(record.get('changedFiles') or [])
        raw = self.engine.git(work, 'diff', '--name-only', '-z', record['base'], '--').decode()
        names = [item for item in raw.split('\0') if item]
        extra = self.engine.git(work, 'ls-files', '-z', '--others', '--exclude-standard').decode().split('\0')
        ignored = self.engine.git(work, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard').decode().split('\0')
        for item in extra + ignored:
            if item and item not in names:
                names.append(item)
        return names

    def mark(self, pid, lid, **fields):
        with self.engine.lock:
            record = self._record(pid, lid)
            record.update(fields)
            record['updatedAt'] = now()
            self._replace(pid, record)
            return self.get(pid, lid)

    def close(self, pid, lid):
        with self.engine.lock:
            root = self.engine.root(pid)
            record = self._record(pid, lid)
            work = Path(record['path'])
            if work.exists():
                if self.engine.git(work, 'status', '--porcelain'):
                    raise Problem('Save or resolve this lane before closing it. Its unsaved files have been preserved.')
                self.engine.git(root, 'worktree', 'remove', str(work))
            record['status'] = 'closed'
            record['updatedAt'] = now()
            self._replace(pid, record)
            return self.get(pid, lid)

    def children(self, pid, lid):
        return [item for item in self._load(pid) if item.get('parent') == lid and item.get('status') != 'closed']
