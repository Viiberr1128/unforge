"""Opt-in, event-driven workspace backups while Unforge is open.

The scheduler records dirty generations independently of backup job receipts.
Only a completed job may acknowledge the generation captured when it started.
"""
import copy
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time

from engine import Problem


class BackupScheduler:
    def __init__(self, engine, backups, *, clock=time.time, debounce_seconds=30,
                 poll_seconds=5, start_thread=True):
        self.engine = engine
        self.backups = backups
        self.clock = clock
        self.debounce_seconds = max(0, float(debounce_seconds))
        self.poll_seconds = max(1, float(poll_seconds))
        self.root = engine.home / '.backups'
        self.root.mkdir(mode=0o700, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise Problem('Backup schedule storage must belong to this account.')
        self.path = self.root / 'schedule.json'
        self.condition = threading.Condition(threading.RLock())
        self.closed = False
        self.thread = None
        self.waiting_for_other = False
        self.blocked_reason = None
        self.persistence_error = None
        self.retry_not_before = 0
        self.recovery_required = False
        self.recovery_error = None
        self.recovery_archive = None
        try:
            self.data = self._load()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # A broken optional schedule must never prevent access to projects.
            # Keep the original bytes and require an explicit settings repair.
            self.data = self._empty()
            self.data['dirtyGeneration'] = 1
            self.recovery_required = True
            self.recovery_error = ('Automatic backups are disabled because their scheduling history cannot be read. '
                                   'The original settings are preserved. Save automatic backup settings to repair them; '
                                   'a new completed backup is required before pending changes can be cleared. ' + str(exc)[:400])
            self.data['lastError'] = 'Previous scheduling history is unavailable. A new completed backup is required.'
        if start_thread:
            self.thread = threading.Thread(target=self._run, name='unforge-backup-scheduler', daemon=False)
            self.thread.start()

    @staticmethod
    def _empty():
        return {'schemaVersion': 1, 'enabled': False, 'intervalSeconds': 300,
                'dirtyGeneration': 0, 'backedUpGeneration': 0,
                'runningJobId': None, 'runningGeneration': None,
                'starting': False, 'lastChangeAt': None, 'lastAttemptAt': None,
                'lastCompletedAt': None, 'retryAfter': None, 'lastError': None,
                'pendingSince': None}

    def _load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            if any(self.root.glob('schedule.invalid-*.json')):
                raise Problem('A previous settings repair did not finish; its original file remains archived.')
            return self._empty()
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 16 * 1024:
                raise Problem('Invalid automatic backup settings file.')
            try:
                value = json.loads(source.read(16 * 1024 + 1))
            except (ValueError, UnicodeError) as exc:
                raise Problem('Automatic backup settings cannot be read. Preserve the settings file before repairing it.') from exc
        if isinstance(value, dict) and 'pendingSince' not in value:
            value['pendingSince'] = value.get('lastChangeAt') if value.get('dirtyGeneration', 0) != value.get('backedUpGeneration', 0) else None
        if not isinstance(value, dict) or value.get('schemaVersion') != 1 or set(value) != set(self._empty()):
            raise Problem('Unsupported automatic backup settings.')
        if type(value['enabled']) is not bool or type(value['starting']) is not bool:
            raise Problem('Invalid automatic backup state.')
        self._interval(value['intervalSeconds'])
        for key in ('dirtyGeneration', 'backedUpGeneration'):
            if type(value[key]) is not int or not 0 <= value[key] <= 2**63 - 1:
                raise Problem('Invalid backup generation.')
        if value['backedUpGeneration'] > value['dirtyGeneration']:
            raise Problem('Invalid completed backup generation.')
        generation = value['runningGeneration']
        if generation is not None and (type(generation) is not int or not 0 <= generation <= value['dirtyGeneration']):
            raise Problem('Invalid running backup generation.')
        if value['runningJobId'] is not None and (not isinstance(value['runningJobId'], str) or not re.fullmatch(r'[a-f0-9]{32}', value['runningJobId'])):
            raise Problem('Invalid scheduled backup job identity.')
        for key in ('lastChangeAt', 'lastAttemptAt', 'lastCompletedAt', 'retryAfter', 'pendingSince'):
            timestamp = value[key]
            if timestamp is not None and (type(timestamp) not in (float, int) or not math.isfinite(timestamp) or timestamp < 0):
                raise Problem('Invalid automatic backup timestamp.')
        if value['lastError'] is not None and (not isinstance(value['lastError'], str) or len(value['lastError']) > 2000):
            raise Problem('Invalid automatic backup result.')
        return value

    def _archive_corrupt(self):
        """Explicit repair preserves even invalid JSON or a link, without following it."""
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISDIR(info.st_mode):
            raise Problem('The automatic backup settings path is a directory. Move it aside before repairing the settings.')
        descriptor, name = tempfile.mkstemp(prefix='schedule.invalid-', suffix='.json', dir=self.root)
        os.close(descriptor)
        archive = Path(name)
        try:
            os.replace(self.path, archive)
        except BaseException:
            archive.unlink(missing_ok=True)
            raise
        self.recovery_archive = str(archive)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _persist(self):
        if self.path.is_symlink():
            raise Problem('Backup schedule cannot be a symbolic link.')
        descriptor, name = tempfile.mkstemp(prefix='.schedule-', dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                json.dump(self.data, output, separators=(',', ':'), ensure_ascii=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.persistence_error = None
        except Exception as exc:
            self.persistence_error = 'Automatic backup progress could not be saved: ' + str(exc)[:500]
            raise
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _interval(value):
        if type(value) is not int or not 60 <= value <= 7 * 24 * 60 * 60:
            raise Problem('Automatic backup interval must be a whole number from 60 seconds to seven days.')
        return value

    def _configured(self):
        try:
            return bool(self.backups.settings().get('configured'))
        except Exception as exc:
            self.blocked_reason = 'Backup destination settings need attention: ' + str(exc)[:300]
            return False

    def _pending(self):
        return self.data['dirtyGeneration'] > self.data['backedUpGeneration']

    def _due(self):
        if not self.data['enabled'] or not self._pending() or self.data['runningJobId']:
            return None
        changes_due = (self.data['lastChangeAt'] or 0) + self.debounce_seconds
        interval = self.data['intervalSeconds']
        if self.data['pendingSince'] is not None:
            # Continuous editing must not postpone every backup forever.
            changes_due = min(changes_due, self.data['pendingSince'] + interval)
        attempt_due = 0 if self.data['lastAttemptAt'] is None else self.data['lastAttemptAt'] + interval
        completion_due = 0 if self.data['lastCompletedAt'] is None else self.data['lastCompletedAt'] + interval
        return max(changes_due, attempt_due, completion_due, self.data['retryAfter'] or 0, self.retry_not_before)

    def state(self):
        with self.condition:
            result = copy.deepcopy(self.data)
            result.pop('schemaVersion')
            result.pop('starting')
            result.update(pending=self._pending(), nextDueAt=self._due(), debounceSeconds=self.debounce_seconds,
                          waitingForOtherBackup=self.waiting_for_other, blockedReason=self.blocked_reason,
                          progressDurable=self.persistence_error is None and not self.recovery_required,
                          persistenceError=self.persistence_error,
                          error=self.recovery_error or self.persistence_error or self.data['lastError'],
                          recoveryRequired=self.recovery_required, recoveryArchive=self.recovery_archive,
                          scope='Changes reported through Unforge while this app is open. External file edits and changes inside running apps are not automatically detected. A verified local copy does not prove a cloud upload.')
            return result

    def configure(self, enabled, interval_seconds=300):
        if type(enabled) is not bool:
            raise Problem('Choose whether automatic backups are enabled.')
        interval = self._interval(interval_seconds)
        with self.condition:
            if self.closed:
                raise Problem('Automatic backup scheduling is closed.')
            if enabled and not self._configured():
                raise Problem('Set up a backup destination and recovery key before enabling automatic backups.')
            repairing = self.recovery_required
            if repairing:
                self._archive_corrupt()
            previous = copy.deepcopy(self.data)
            first_enable = enabled and not self.data['enabled']
            self.data.update(enabled=enabled, intervalSeconds=interval)
            if first_enable:
                if not self._pending():
                    self.data['pendingSince'] = max(0, self.clock() - self.debounce_seconds)
                self.data['dirtyGeneration'] += 1
                # The first backup is immediately due; later edits debounce.
                self.data['lastChangeAt'] = max(0, self.clock() - self.debounce_seconds)
            self.blocked_reason = None
            try:
                self._persist()
            except Exception:
                self.data = previous
                raise
            if repairing:
                self.recovery_required = False
                self.recovery_error = None
            self.condition.notify_all()
            return self.state()

    def changed(self):
        with self.condition:
            if self.closed:
                return self.state()
            if not self._pending():
                self.data['pendingSince'] = self.clock()
            self.data['dirtyGeneration'] += 1
            self.data['lastChangeAt'] = self.clock()
            if not self.recovery_required:
                self._persist()
            self.condition.notify_all()
            return self.state()

    def _busy(self):
        worker = getattr(self.backups, 'worker', None)
        return worker is not None and worker.is_alive()

    def _job(self, jobid):
        lock = getattr(self.backups, 'lock', None)
        if lock is None:
            return copy.deepcopy(self.backups.jobs.get(jobid))
        with lock:
            return copy.deepcopy(self.backups.jobs.get(jobid))

    def _step(self):
        """One deterministic transition. Called with the condition held by the worker."""
        with self.condition:
            if self.closed:
                return None
            now = self.clock()
            self.waiting_for_other = False
            if self.data['runningJobId']:
                job = self._job(self.data['runningJobId'])
                if job and job.get('state') == 'running' and self._busy():
                    return self.poll_seconds
                previous = copy.deepcopy(self.data)
                if job and job.get('state') == 'completed':
                    captured = self.data['runningGeneration']
                    if captured is not None:
                        self.data['backedUpGeneration'] = max(self.data['backedUpGeneration'], captured)
                    if not self._pending():
                        self.data['pendingSince'] = None
                    self.data.update(lastCompletedAt=now, lastError=None, retryAfter=None)
                else:
                    self.data.update(lastError=(str(job.get('error') or 'The automatic backup did not complete.') if job else 'The automatic backup outcome is unavailable. Pending changes remain scheduled.')[:2000],
                                     retryAfter=now + self.data['intervalSeconds'])
                self.data.update(runningJobId=None, runningGeneration=None, starting=False)
                try:
                    self._persist()
                except Exception:
                    # A failed acknowledgement must leave the captured
                    # generation pending in memory as well as on disk.
                    self.data = previous
                    raise
            elif self.data['starting']:
                # Process stopped after durable intent but before the returned
                # job identity was recorded. Do not guess which receipt is ours.
                self.data.update(starting=False, runningGeneration=None,
                                 lastError='The last backup start was interrupted. Pending changes were preserved.',
                                 retryAfter=now + self.data['intervalSeconds'])
                self._persist()
            if not self.data['enabled'] or not self._pending():
                self.blocked_reason = None
                return None
            if not self._configured():
                self.blocked_reason = self.blocked_reason or 'Set up a backup destination and recovery key.'
                return None
            if self._busy():
                self.waiting_for_other = True
                self.blocked_reason = 'Waiting for the current backup or restore to finish.'
                return self.poll_seconds
            self.blocked_reason = None
            due = self._due()
            if due is None:
                return None
            if now < due:
                return due - now
            previous = copy.deepcopy(self.data)
            self.data.update(starting=True, runningGeneration=self.data['dirtyGeneration'],
                             lastAttemptAt=now, lastError=None)
            try:
                self._persist()
            except Exception:
                self.data = previous
                raise
            try:
                job = self.backups.start()
                if not isinstance(job, dict) or not isinstance(job.get('id'), str) or not re.fullmatch(r'[a-f0-9]{32}', job['id']):
                    raise Problem('Backup service returned no usable job identity.')
                self.data.update(runningJobId=job['id'], starting=False)
                self._persist()
            except Exception as exc:
                # Even if start succeeded and its receipt write failed, never
                # clear the generation; a live worker prevents duplicate start.
                self.data.update(starting=False, lastError=str(exc)[:2000], retryAfter=now + self.data['intervalSeconds'])
                if not self.data['runningJobId']:
                    self.data['runningGeneration'] = None
                self._persist()
                return self.poll_seconds if self._busy() else self.data['intervalSeconds']
            return self.poll_seconds

    def _run(self):
        with self.condition:
            while not self.closed:
                try:
                    delay = self._step()
                except Exception as exc:
                    self.data['lastError'] = str(exc)[:2000]
                    self.retry_not_before = self.clock() + self.data['intervalSeconds']
                    delay = self.data['intervalSeconds']
                if not self.closed:
                    self.condition.wait(timeout=delay)

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise Problem('Automatic backup scheduling is still stopping.')
