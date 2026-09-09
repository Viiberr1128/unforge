"""Durable allowance and duplicate-action ledger for explicitly routed local work.

This is not a provider billing cap or protection for actions outside this ledger.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import uuid

SCOPE = 'Applies only to actions routed through this local Unforge ledger. Units are attempts, not money, tokens, or a provider-wide spending cap.'
KINDS = {'agent', 'email', 'payment', 'webhook'}
MAX_PAYLOAD = 32768


class OperationError(ValueError):
    pass


def stamp():
    return datetime.now(timezone.utc).isoformat()


def day():
    return datetime.now(timezone.utc).date().isoformat()


def encoded(value):
    try:
        text = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        raise OperationError('Use a JSON-compatible value.')
    if len(text.encode('utf-8')) > MAX_PAYLOAD:
        raise OperationError('Payload or result exceeds 32 KiB.')
    return text


def identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise OperationError(f'{label} must be 1–128 letters, digits, dots, colons, underscores, or hyphens.')
    return value


class Operations:
    def __init__(self, home):
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / 'operations.sqlite3'
        if self.path.is_symlink():
            raise OperationError('Ledger must not be a symbolic link.')
        self.session = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._closed = False
        self.owners = self.home / 'operation-owners'
        if self.owners.is_symlink():
            raise OperationError('Ledger owner directory must not be a symbolic link.')
        self.owners.mkdir(mode=0o700, exist_ok=True)
        self.owner_path = self.owners / (self.session + '.lock')
        self._owner = self._lease(self.owner_path)
        fcntl.flock(self._owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    daily_limit INTEGER NOT NULL, revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO settings VALUES(1, 20, 1);
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    state TEXT NOT NULL, units INTEGER NOT NULL, utc_day TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    result_json TEXT, owner_pid INTEGER NOT NULL, owner_session TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS by_day ON operations(utc_day);
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
                    paused INTEGER NOT NULL DEFAULT 0, resumed_at TEXT);
                CREATE TABLE IF NOT EXISTS reconciliations (
                    id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL,
                    previous_state TEXT NOT NULL, outcome TEXT NOT NULL,
                    note TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            try:
                if 'reference_id' not in {row['name'] for row in db.execute('PRAGMA table_info(operations)')}:
                    db.execute('ALTER TABLE operations ADD COLUMN reference_id TEXT')
                db.execute('CREATE UNIQUE INDEX IF NOT EXISTS by_reference ON operations(reference_id)')
                db.execute('CREATE INDEX IF NOT EXISTS by_intent ON operations(project_id,kind,payload_hash,state)')
                db.execute('CREATE INDEX IF NOT EXISTS by_state ON operations(state,created_at)')
                db.commit()
            except BaseException:
                db.rollback()
                raise
        self.path.chmod(0o600)
        with self._transaction() as db:
            self._recover(db)

    @staticmethod
    def _lease(path):
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            os.close(fd)
            raise OperationError('Ledger owner lease is not an ordinary file owned by this account.')
        return os.fdopen(fd, 'a')

    def _recover(self, db):
        # A held OS lock proves a live ledger owner, even when a PID was reused.
        # Another instance in this same process must not claim a live operation.
        running = db.execute("SELECT * FROM operations WHERE state='running'").fetchall()
        for row in running:
            if row['owner_session'] == self.session:
                continue
            session = row['owner_session']
            if not re.fullmatch(r'[a-f0-9]{32}', session):
                raise OperationError('Invalid ledger owner session.')
            lease_path = self.owners / (session + '.lock')
            with self._lease(lease_path) as lease:
                try:
                    fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                result = json.loads(row['result_json']) if row['result_json'] else {}
                if not isinstance(result, dict):
                    result = {'previousResult': result}
                result['message'] = 'Execution owner stopped before recording an outcome. Reconcile before retrying.'
                try:
                    recovered = encoded(result)
                except OperationError:
                    recovered = encoded({'message': result['message'], 'previousResultOmitted': True})
                db.execute("UPDATE operations SET state='unknown', updated_at=?, result_json=? WHERE operation_id=?",
                           (stamp(), recovered, row['operation_id']))
                self._failure(db, row['project_id'])
            lease_path.unlink(missing_ok=True)

    def close(self):
        """Release ownership after routed workers have stopped; never imply success."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._owner.close()
                self.owner_path.unlink(missing_ok=True)

    def __del__(self):
        if hasattr(self, '_owner'):
            self._owner.close()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._lock, self._db() as db:
            if self._closed:
                raise OperationError('The operation ledger is closed.')
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _settings(self, db):
        row = db.execute('SELECT * FROM settings WHERE singleton=1').fetchone()
        return {'dailyLimit': row['daily_limit'], 'revision': row['revision'], 'unit': 'attempt', 'dayBoundary': 'UTC', 'scope': SCOPE}

    def settings(self):
        with self._db() as db:
            return self._settings(db)

    def configure(self, daily_limit, expected_revision):
        if isinstance(daily_limit, bool) or not isinstance(daily_limit, int) or not 0 <= daily_limit <= 100000:
            raise OperationError('Daily limit must be an integer between 0 and 100,000 attempts.')
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise OperationError('Supply the settings revision you read.')
        with self._transaction() as db:
            current = self._settings(db)
            if current['revision'] != expected_revision:
                raise OperationError('Allowance settings changed. Refresh before saving.')
            db.execute('UPDATE settings SET daily_limit=?, revision=revision+1 WHERE singleton=1', (daily_limit,))
            return self._settings(db)

    def _record(self, row, replayed=False):
        return {'operationId': row['operation_id'], 'projectId': row['project_id'], 'kind': row['kind'],
                'payloadHash': row['payload_hash'], 'state': row['state'], 'units': row['units'],
                'referenceId': row['reference_id'],
                'utcDay': row['utc_day'], 'createdAt': row['created_at'], 'updatedAt': row['updated_at'],
                'result': json.loads(row['result_json']) if row['result_json'] else None, 'replayed': replayed}

    def overview(self):
        with self._transaction() as db:
            self._recover(db)
            settings = self._settings(db)
            used = db.execute('SELECT COALESCE(SUM(units),0) FROM operations WHERE utc_day=?', (day(),)).fetchone()[0]
            rows = db.execute('SELECT * FROM operations ORDER BY created_at DESC, rowid DESC LIMIT 100').fetchall()
            unknown_count = db.execute("SELECT COUNT(*) FROM operations WHERE state='unknown'").fetchone()[0]
            unknowns = db.execute("SELECT * FROM operations WHERE state='unknown' ORDER BY created_at, rowid LIMIT 100").fetchall()
            projects = db.execute('SELECT * FROM projects WHERE paused=1 ORDER BY project_id LIMIT 100').fetchall()
            return {'settings': settings, 'usedToday': used, 'remainingToday': max(0, settings['dailyLimit'] - used),
                    'utcDay': day(), 'operations': [self._record(r) for r in rows], 'scope': SCOPE,
                    'unknownOperations': [self._record(r) for r in unknowns], 'unknownCount': unknown_count,
                    'pausedProjects': [{'projectId': r['project_id'], 'consecutiveFailures': r['failures']} for r in projects],
                    'historyLimit': 100}

    def _binding(self, project_id, operation_id, kind, payload):
        identifier(project_id, 'Project ID')
        identifier(operation_id, 'Operation ID')
        if not isinstance(kind, str) or kind not in KINDS:
            raise OperationError('Unsupported operation kind.')
        if not isinstance(payload, dict):
            raise OperationError('Payload must be a JSON object.')
        return hashlib.sha256(encoded(payload).encode('utf-8')).hexdigest()

    def _existing(self, db, project_id, operation_id, kind, payload_hash):
        old = db.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
        if old:
            if old['project_id'] != project_id or old['kind'] != kind or old['payload_hash'] != payload_hash:
                raise OperationError('Operation ID is already bound to a different project, kind, or payload.')
            return self._record(old, True)
        return None

    def lookup(self, project_id, operation_id, kind, payload):
        """Check a replay before agent preflight, without reserving an attempt."""
        payload_hash = self._binding(project_id, operation_id, kind, payload)
        with self._transaction() as db:
            self._recover(db)
            return self._existing(db, project_id, operation_id, kind, payload_hash)

    def by_reference(self, reference_id):
        identifier(reference_id, 'Reference ID')
        with self._transaction() as db:
            self._recover(db)
            row = db.execute('SELECT * FROM operations WHERE reference_id=?', (reference_id,)).fetchone()
            return self._record(row, True) if row else None

    def get(self, operation_id):
        identifier(operation_id, 'Operation ID')
        with self._transaction() as db:
            self._recover(db)
            row = db.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
            if not row:
                raise OperationError('Unknown operation ID.')
            return self._record(row)

    def reserve(self, project_id, operation_id, kind, payload, *, reference_id=None, initial_result=None):
        payload_hash = self._binding(project_id, operation_id, kind, payload)
        if reference_id is not None:
            identifier(reference_id, 'Reference ID')
        result = encoded(initial_result) if initial_result is not None else None
        with self._transaction() as db:
            self._recover(db)
            old = self._existing(db, project_id, operation_id, kind, payload_hash)
            if old:
                return old
            if db.execute("SELECT 1 FROM operations WHERE project_id=? AND kind=? AND payload_hash=? AND state='unknown' LIMIT 1",
                          (project_id, kind, payload_hash)).fetchone():
                raise OperationError('An earlier attempt with this same intent has an unknown outcome. Reconcile it before starting another attempt.')
            if reference_id is not None and db.execute('SELECT 1 FROM operations WHERE reference_id=?', (reference_id,)).fetchone():
                raise OperationError('Reference ID is already bound to a different operation.')
            project = db.execute('SELECT * FROM projects WHERE project_id=?', (project_id,)).fetchone()
            if project and project['paused']:
                raise OperationError('This project is paused after three failed, empty, or unknown outcomes. Review it and explicitly resume.')
            settings = self._settings(db)
            today = day()
            used = db.execute('SELECT COALESCE(SUM(units),0) FROM operations WHERE utc_day=?', (today,)).fetchone()[0]
            if used >= settings['dailyLimit']:
                raise OperationError('Daily local allowance reached. No new operation was started.')
            when = stamp()
            db.execute('INSERT OR IGNORE INTO projects(project_id) VALUES(?)', (project_id,))
            db.execute('INSERT INTO operations(operation_id,project_id,kind,payload_hash,state,units,utc_day,created_at,updated_at,result_json,owner_pid,owner_session,reference_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (operation_id, project_id, kind, payload_hash, 'running', 1, today, when, when, result, os.getpid(), self.session, reference_id))
            return self._record(db.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone())

    def _failure(self, db, project_id):
        db.execute('UPDATE projects SET failures=failures+1, paused=CASE WHEN failures+1>=3 THEN 1 ELSE paused END WHERE project_id=?', (project_id,))

    def finish(self, operation_id, state, result):
        identifier(operation_id, 'Operation ID')
        if state not in ('succeeded', 'failed', 'empty', 'unknown'):
            raise OperationError('Outcome must be succeeded, failed, empty, or unknown.')
        data = encoded(result)
        with self._transaction() as db:
            old = db.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
            if not old:
                raise OperationError('Unknown operation ID.')
            if old['state'] != 'running':
                if old['state'] == state and old['result_json'] == data:
                    return self._record(old, True)
                raise OperationError('Outcome already recorded. Only unknown outcomes can be explicitly reconciled.')
            if old['owner_session'] != self.session:
                raise OperationError('Only the execution owner can record its running outcome.')
            db.execute('UPDATE operations SET state=?, result_json=?, updated_at=? WHERE operation_id=?',
                       (state, data, stamp(), operation_id))
            if state in ('failed', 'empty', 'unknown'):
                self._failure(db, old['project_id'])
            else:
                db.execute('UPDATE projects SET failures=0 WHERE project_id=? AND paused=0', (old['project_id'],))
            return self._record(db.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone())

    def reconcile(self, operation_id, outcome, note):
        identifier(operation_id, 'Operation ID')
        if outcome not in ('succeeded', 'failed'):
            raise OperationError('Reconciled outcome must be succeeded or failed.')
        if not isinstance(note, str) or not note.strip() or len(note) > 2000:
            raise OperationError('Explain the reconciliation in 1–2,000 characters.')
        with self._transaction() as db:
            self._recover(db)
            old = db.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone()
            if not old or old['state'] != 'unknown':
                raise OperationError('Only unknown outcomes can be reconciled.')
            when = stamp()
            result = {'previousResult': json.loads(old['result_json']) if old['result_json'] else None,
                      'reconciled': True, 'note': note.strip()}
            db.execute('INSERT INTO reconciliations(operation_id,previous_state,outcome,note,created_at) VALUES(?,?,?,?,?)',
                       (operation_id, 'unknown', outcome, note.strip(), when))
            db.execute('UPDATE operations SET state=?, result_json=?, updated_at=? WHERE operation_id=?',
                       (outcome, encoded(result), when, operation_id))
            # Reconciliation is not a new execution and never clears a project pause.
            return self._record(db.execute('SELECT * FROM operations WHERE operation_id=?', (operation_id,)).fetchone())

    def resume(self, project_id):
        identifier(project_id, 'Project ID')
        with self._transaction() as db:
            self._recover(db)
            if db.execute("SELECT 1 FROM operations WHERE project_id=? AND state='unknown' LIMIT 1", (project_id,)).fetchone():
                raise OperationError('Reconcile this project\'s unknown outcomes before resuming it.')
            when = stamp()
            db.execute('INSERT OR IGNORE INTO projects(project_id) VALUES(?)', (project_id,))
            db.execute('UPDATE projects SET failures=0, paused=0, resumed_at=? WHERE project_id=?', (when, project_id))
            return {'projectId': project_id, 'paused': False, 'consecutiveFailures': 0, 'resumedAt': when}

    def practice(self, project_id, operation_id, action, payload):
        if not isinstance(action, str) or action not in ('email', 'payment', 'webhook'):
            raise OperationError('Practice action must be email, payment, or webhook.')
        reservation = self.reserve(project_id, operation_id, action, payload)
        if reservation['replayed']:
            return reservation
        # An intentionally local receipt: no network, money movement, or message delivery.
        receipt = {'simulated': True, 'action': action, 'receiptId': operation_id,
                   'message': {'email': 'Practice email recorded locally. Nothing was sent.',
                               'payment': 'Practice payment recorded locally. No money moved.',
                               'webhook': 'Practice webhook recorded locally. No request was sent.'}[action]}
        return self.finish(operation_id, 'succeeded', receipt)
