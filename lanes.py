"""Durable Git worktrees so people and agents can commit without sharing one checkout."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from engine import MAX_TEXT, Problem

BRANCH_PREFIX = 'unforge-lane-'
MAX_LANES = 32
MAX_NAME = 80


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
    finally:
        Path(temporary).unlink(missing_ok=True)


class Lanes:
    def __init__(self, engine):
        self.engine = engine
        self.root = engine.home / '.lanes'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _index_path(self, pid):
        return self.root / (pid + '.json')

    def _work(self, pid, lid):
        return self.root / pid / lid

    def _load(self, pid):
        path = self._index_path(pid)
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, list):
            raise Problem('Lane index is damaged. Existing worktrees have been left untouched.')
        return data

    def _save(self, pid, records):
        atomic_json(self._index_path(pid), records)

    def _record(self, pid, lid):
        if not isinstance(lid, str) or not re.fullmatch(r'[a-f0-9]{32}', lid):
            raise Problem('Unknown lane')
        for item in self._load(pid):
            if item.get('id') == lid:
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

    def _refresh(self, pid, record):
        work = Path(record['path'])
        if work.is_dir() and (work / '.git').exists() or work.is_dir() and (work / '.git').is_file():
            try:
                record['head'] = self._head(work)
                record['dirty'] = bool(self.engine.git(work, 'status', '--porcelain'))
                record['changedFiles'] = self.changed_files(pid, record['id'])
            except Problem:
                record['dirty'] = True
        return record

    def _public(self, pid, record):
        item = self._refresh(pid, dict(record))
        return {key: item.get(key) for key in (
            'id', 'name', 'status', 'parent', 'base', 'head', 'branch', 'path',
            'claimedPaths', 'changedFiles', 'dirty', 'createdAt', 'updatedAt', 'conflictFiles')}

    def list(self, pid):
        self.engine.root(pid)
        return {'lanes': [self._public(pid, item) for item in self._load(pid)]}

    def get(self, pid, lid):
        self.engine.root(pid)
        return self._public(pid, self._record(pid, lid))

    def create(self, pid, name, parent=None, claimed_paths=None):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME:
            raise Problem('Lane name must contain 1–80 characters')
        paths = claimed_paths or []
        if not isinstance(paths, list) or len(paths) > 64 or any(
                not isinstance(item, str) or not item or len(item) > 240 for item in paths):
            raise Problem('Claimed paths must be a short list of project-relative files')
        with self.engine.lock:
            root = self.engine.root(pid)
            if self.engine.git(root, 'status', '--porcelain'):
                raise Problem('Save your current changes before opening a lane')
            records = self._load(pid)
            if len([item for item in records if item.get('status') in ('open', 'needs_restack', 'conflict')]) >= MAX_LANES:
                raise Problem('This project already has 32 active lanes. Merge or close one first.')
            live = self._head(root)
            parent_id = None
            base = live
            if parent:
                parent_record = self._record(pid, parent)
                parent_id = parent_record['id']
                if parent_record.get('status') == 'merged':
                    base = live
                else:
                    base = parent_record['head']
            lid = uuid.uuid4().hex
            work = self._work(pid, lid)
            if work.exists():
                raise Problem('Lane workspace already exists')
            work.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            branch = BRANCH_PREFIX + lid
            try:
                self.engine.git(root, 'worktree', 'add', '-b', branch, str(work), base)
            except Problem:
                shutil.rmtree(work, ignore_errors=True)
                raise
            record = dict(id=lid, name=name.strip(), status='open', parent=parent_id, base=base,
                          head=self._head(work), branch=branch, path=str(work), claimedPaths=list(paths),
                          changedFiles=[], dirty=False, createdAt=now(), updatedAt=now(), conflictFiles=[])
            self._replace(pid, record)
            return self._public(pid, record)

    def write(self, pid, lid, path, content):
        if not isinstance(content, str) or len(content.encode()) > MAX_TEXT or '\0' in content:
            raise Problem('File must be text smaller than 128 KiB')
        with self.engine.lock:
            record = self._record(pid, lid)
            if record.get('status') not in ('open', 'needs_restack', 'conflict'):
                raise Problem('This lane can no longer be edited')
            work = Path(record['path'])
            target = self.engine.safe_path(work, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
            record['updatedAt'] = now()
            record['status'] = 'open' if record.get('status') != 'conflict' else record['status']
            self._replace(pid, record)
            return self.get(pid, lid)

    def save(self, pid, lid, message):
        if not isinstance(message, str) or not message.strip() or len(message) > 500:
            raise Problem('Version description must contain 1–500 characters')
        with self.engine.lock:
            record = self._record(pid, lid)
            if record.get('status') not in ('open', 'needs_restack', 'conflict'):
                raise Problem('This lane can no longer be saved')
            work = Path(record['path'])
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
        raw = self.engine.git(work, 'diff', '--name-only', '-z', record['base'] + '...HEAD').decode()
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
                self.engine.git(root, 'worktree', 'remove', '--force', str(work), allowed_returncodes=(0, 128))
            shutil.rmtree(work, ignore_errors=True)
            record['status'] = 'closed'
            record['updatedAt'] = now()
            self._replace(pid, record)
            return self.get(pid, lid)

    def children(self, pid, lid):
        return [item for item in self._load(pid) if item.get('parent') == lid and item.get('status') != 'closed']
