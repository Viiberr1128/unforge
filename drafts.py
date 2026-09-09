"""Private, durable editor drafts. Drafts never mutate a project's Git checkout."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from engine import MAX_TEXT, Problem

MAX_DRAFTS = 200
MAX_DRAFT_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 2 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def content_hash(value):
    return None if value is None else hashlib.sha256(value.encode('utf-8')).hexdigest()


def validate_text(value, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or '\0' in value:
        raise Problem('Draft content must be UTF-8 text without null bytes.')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError as exc:
        raise Problem('Draft content must be valid UTF-8 text.') from exc
    if size > MAX_TEXT:
        raise Problem('Draft content must be text no larger than 128 KiB.')


class Drafts:
    def __init__(self, engine):
        self.engine = engine
        self._store = engine.home / '.drafts'
        self._directory(self._store)

    @staticmethod
    def _directory(path):
        created = False
        try:
            path.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise Problem('Draft storage must be a directory owned by this account.')
        path.chmod(0o700)
        if created:
            Drafts._sync_directory(path.parent)

    def _validate_path(self, path):
        # Validate syntax and reserved names independently of the current source
        # file. A draft must remain recoverable if that source file is replaced
        # by a symlink, a directory, or a file the editor cannot read anymore.
        self.engine.safe_path(self._store, path)
        if path.lower() == '.unforge/project.json':
            raise Problem('Project metadata is managed by Unforge.')
        return hashlib.sha256(path.encode('utf-8')).hexdigest() + '.json'

    @contextmanager
    def _transaction(self, pid):
        with self.engine.lock:
            root = self.engine.root(pid)
            self._directory(self._store)
            folder = self._store / pid
            self._directory(folder)
            fd = os.open(folder / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise Problem('Invalid draft storage lock.')
                fcntl.flock(fd, fcntl.LOCK_EX)
                # A previous interrupted writer cannot still be active after
                # this per-project lock is acquired. Do not follow links.
                for unfinished in folder.glob('.write-*'):
                    entry = unfinished.lstat()
                    if stat.S_ISREG(entry.st_mode) and entry.st_uid == os.getuid():
                        unfinished.unlink()
                yield root, folder
            finally:
                os.close(fd)

    @staticmethod
    def _revision(record):
        return hashlib.sha256(canonical({key: value for key, value in record.items() if key != 'revision'})).hexdigest()

    def _read(self, destination, path=None):
        try:
            fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > MAX_RECORD_BYTES:
                raise Problem('Invalid saved draft file.')
            try:
                record = json.loads(source.read(MAX_RECORD_BYTES + 1))
            except (UnicodeError, ValueError) as exc:
                raise Problem('This draft cannot be read. Preserve a copy of the draft file before repairing it.') from exc
        if not isinstance(record, dict) or record.get('schemaVersion') != 1:
            raise Problem('Unsupported saved draft format.')
        if (not isinstance(record.get('path'), str) or self._validate_path(record['path']) != destination.name or
                (path is not None and record['path'] != path)):
            raise Problem('Saved draft path does not match.')
        if (not isinstance(record.get('generation'), str) or
                not re.fullmatch(r'[a-f0-9]{32}', record['generation']) or
                not isinstance(record.get('updatedAt'), str)):
            raise Problem('Invalid saved draft metadata.')
        validate_text(record.get('content'))
        if 'baseContent' not in record:
            raise Problem('Saved draft is missing its original content.')
        validate_text(record['baseContent'], nullable=True)
        if record.get('baseHash') != content_hash(record['baseContent']) or record.get('revision') != self._revision(record):
            raise Problem('Saved draft checksum does not match. Preserve the file before repairing it.')
        return record

    @staticmethod
    def _expected(expected_revision, nullable=True):
        if expected_revision is None and nullable:
            return
        if not isinstance(expected_revision, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_revision):
            raise Problem('Use the current draft revision before changing or discarding it.')

    def _current(self, root, path):
        try:
            target = self.engine.safe_path(root, path)
            if not target.exists():
                return None, True
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TEXT:
                    return None, False
                raw = source.read(MAX_TEXT + 1)
            value = raw.decode('utf-8')
            if len(raw) > MAX_TEXT or '\0' in value:
                return None, False
            return content_hash(value), True
        except (OSError, ValueError, UnicodeError):
            return None, False

    def _public(self, root, record, include_content=True):
        result = {key: record[key] for key in ('path', 'baseHash', 'revision', 'updatedAt')}
        if include_content:
            result.update(content=record['content'], baseContent=record['baseContent'])
        current, readable = self._current(root, record['path'])
        result.update(currentHash=current, currentReadable=readable,
                      stale=not readable or current != record['baseHash'])
        return result

    @staticmethod
    def _sync_directory(folder):
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def list(self, pid):
        with self._transaction(pid) as (root, folder):
            records = []
            errors = []
            paths = sorted(folder.glob('*.json'))
            if len(paths) > MAX_DRAFTS:
                raise Problem('Draft storage exceeds its record limit. Preserve this folder before repairing it.')
            for path in paths:
                try:
                    record = self._read(path)
                    if record:
                        records.append(self._public(root, record, include_content=False))
                except (OSError, ValueError, TypeError) as exc:
                    errors.append({'id': path.stem, 'error': str(exc)[:300]})
            return {'drafts': sorted(records, key=lambda item: item['path']), 'errors': errors,
                    'limits': {'maxDrafts': MAX_DRAFTS, 'maxContentBytes': MAX_TEXT, 'maxBytes': MAX_DRAFT_BYTES}}

    def get(self, pid, path):
        filename = self._validate_path(path)
        with self._transaction(pid) as (root, folder):
            record = self._read(folder / filename, path)
            return None if record is None else self._public(root, record)

    def save(self, pid, path, content, base_content, expected_revision):
        filename = self._validate_path(path)
        validate_text(content)
        validate_text(base_content, nullable=True)
        self._expected(expected_revision)
        with self._transaction(pid) as (root, folder):
            destination = folder / filename
            old = self._read(destination, path)
            if (None if old is None else old['revision']) != expected_revision:
                raise Problem('This draft changed in another window. Reload its saved version before writing over it.')
            # A repeated exact save needs no extra disk write or revision churn.
            if old is not None and old['content'] == content and old['baseContent'] == base_content:
                return self._public(root, old)
            record = {'schemaVersion': 1, 'path': path, 'content': content,
                      'baseContent': base_content, 'baseHash': content_hash(base_content),
                      'updatedAt': datetime.now(timezone.utc).isoformat(), 'generation': uuid.uuid4().hex}
            record['revision'] = self._revision(record)
            raw = canonical(record)
            if len(raw) > MAX_RECORD_BYTES:
                raise Problem('Draft exceeds its saved record limit.')
            existing = list(folder.glob('*.json'))
            if old is None and len(existing) >= MAX_DRAFTS:
                raise Problem('This project has 200 saved drafts. Write or discard a draft before creating another.')
            total = sum(item.lstat().st_size for item in existing if item != destination)
            if total + len(raw) > MAX_DRAFT_BYTES:
                raise Problem('This project reached its draft storage limit. Existing drafts have been preserved.')
            descriptor, name = tempfile.mkstemp(prefix='.write-', dir=folder)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, 'wb') as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
                self._sync_directory(folder)
            finally:
                temporary.unlink(missing_ok=True)
            return self._public(root, record)

    def discard(self, pid, path, expected_revision):
        filename = self._validate_path(path)
        self._expected(expected_revision, nullable=False)
        with self._transaction(pid) as (_, folder):
            destination = folder / filename
            old = self._read(destination, path)
            if old is None or old['revision'] != expected_revision:
                raise Problem('This draft changed or was already discarded. Reload before discarding it.')
            destination.unlink()
            self._sync_directory(folder)
            return {'path': path, 'discarded': True}
