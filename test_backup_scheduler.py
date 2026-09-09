import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from backup_scheduler import BackupScheduler
from engine import Engine, Problem


class Clock:
    def __init__(self):
        self.value = 1000.0
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += seconds


class FakeWorker:
    def __init__(self):
        self.alive = False
    def is_alive(self):
        return self.alive


class FakeBackups:
    def __init__(self):
        self.configured = True
        self.jobs = {}
        self.worker = FakeWorker()
        self.lock = threading.RLock()
        self.starts = 0
        self.fail_start = False
    def settings(self):
        return {'configured': self.configured}
    def start(self):
        if self.worker.alive:
            raise Problem('Backup already running')
        if self.fail_start:
            raise Problem('Destination disconnected')
        self.starts += 1
        job = {'id': uuid.uuid4().hex, 'state': 'running'}
        self.jobs[job['id']] = job
        self.worker.alive = True
        return dict(job)
    def finish(self, state='completed'):
        for job in self.jobs.values():
            if job['state'] == 'running':
                job['state'] = state
                if state != 'completed':
                    job['error'] = 'Copy failed'
        self.worker.alive = False


class BackupSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(Path(self.temp.name) / 'home')
        self.backups = FakeBackups()
        self.clock = Clock()
        self.scheduler = BackupScheduler(self.engine, self.backups, clock=self.clock, start_thread=False)
    def tearDown(self):
        self.scheduler.close()
        self.temp.cleanup()
    def restart(self):
        self.scheduler.close()
        self.scheduler = BackupScheduler(self.engine, self.backups, clock=self.clock, start_thread=False)
    def enable_and_start(self):
        self.scheduler.configure(True, 300)
        self.scheduler._step()
        self.assertEqual(self.backups.starts, 1)
    def complete_initial(self):
        self.enable_and_start()
        self.backups.finish()
        self.scheduler._step()
        self.assertFalse(self.scheduler.state()['pending'])

    def test_disabled_until_explicitly_enabled_and_initial_backup_is_due(self):
        self.scheduler.changed()
        self.assertIsNone(self.scheduler._step())
        self.assertEqual(self.backups.starts, 0)
        self.assertFalse(self.scheduler.state()['enabled'])
        self.scheduler.configure(True, 300)
        self.assertEqual(self.scheduler.state()['nextDueAt'], self.clock())
        self.scheduler._step()
        self.assertEqual(self.backups.starts, 1)
        self.assertTrue(self.scheduler.state()['pending'])

    def test_configuration_requires_destination_and_valid_interval(self):
        self.backups.configured = False
        with self.assertRaisesRegex(Problem, 'destination'):
            self.scheduler.configure(True, 300)
        self.assertFalse(self.scheduler.state()['enabled'])
        self.backups.configured = True
        for value in (59, True, '300', 700000):
            with self.subTest(interval=value), self.assertRaises(Problem):
                self.scheduler.configure(True, value)
        self.scheduler.configure(True, 60)
        self.assertEqual(self.scheduler.state()['intervalSeconds'], 60)

    def test_debounce_coalesces_changes_and_minimum_interval_is_respected(self):
        self.complete_initial()
        self.clock.advance(280)
        self.scheduler.changed()
        self.clock.advance(15)
        self.scheduler.changed()
        self.clock.advance(20)
        self.assertEqual(self.scheduler._step(), 10)
        self.assertEqual(self.backups.starts, 1)
        self.clock.advance(10)
        self.scheduler._step()
        self.assertEqual(self.backups.starts, 2)

    def test_success_only_acknowledges_generation_captured_at_start(self):
        self.enable_and_start()
        captured = self.scheduler.state()['runningGeneration']
        self.scheduler.changed()
        self.backups.finish()
        self.scheduler._step()
        state = self.scheduler.state()
        self.assertEqual(state['backedUpGeneration'], captured)
        self.assertGreater(state['dirtyGeneration'], captured)
        self.assertTrue(state['pending'])
        self.assertEqual(self.backups.starts, 1)
        self.clock.advance(300)
        self.scheduler._step()
        self.assertEqual(self.backups.starts, 2)
        self.backups.finish()
        self.scheduler._step()
        self.assertFalse(self.scheduler.state()['pending'])

    def test_continuous_changes_cannot_debounce_backups_forever(self):
        self.complete_initial()
        self.clock.advance(1)
        self.scheduler.changed()
        for _ in range(15):
            self.clock.advance(20)
            self.scheduler.changed()
        self.scheduler._step()
        self.assertEqual(self.backups.starts, 2)

    def test_failed_copy_never_marks_clean_and_retry_is_bounded(self):
        self.enable_and_start()
        self.backups.finish('failed')
        self.scheduler._step()
        self.assertTrue(self.scheduler.state()['pending'])
        self.assertEqual(self.scheduler.state()['backedUpGeneration'], 0)
        self.assertIn('Copy failed', self.scheduler.state()['lastError'])
        self.clock.advance(299)
        self.scheduler._step()
        self.assertEqual(self.backups.starts, 1)
        self.clock.advance(1)
        self.scheduler._step()
        self.assertEqual(self.backups.starts, 2)

    def test_restart_preserves_pending_generation_and_completed_job_receipt(self):
        self.enable_and_start()
        self.scheduler.changed()
        before = self.scheduler.state()
        self.backups.finish()
        self.restart()
        self.scheduler._step()
        after = self.scheduler.state()
        self.assertEqual(after['dirtyGeneration'], before['dirtyGeneration'])
        self.assertEqual(after['backedUpGeneration'], before['runningGeneration'])
        self.assertTrue(after['pending'])
        self.assertEqual(self.backups.starts, 1)

    def test_interrupted_job_after_restart_keeps_pending_without_immediate_duplicate(self):
        self.enable_and_start()
        self.backups.finish('interrupted')
        self.restart()
        self.scheduler._step()
        self.assertTrue(self.scheduler.state()['pending'])
        self.assertEqual(self.scheduler.state()['backedUpGeneration'], 0)
        self.assertEqual(self.backups.starts, 1)

    def test_manual_backup_or_restore_is_not_duplicated_or_claimed_as_our_capture(self):
        self.backups.start()
        self.scheduler.configure(True, 300)
        self.assertEqual(self.scheduler._step(), 5)
        self.assertTrue(self.scheduler.state()['waitingForOtherBackup'])
        self.assertEqual(self.backups.starts, 1)
        self.backups.finish()
        self.scheduler._step()
        self.assertEqual(self.backups.starts, 2)
        self.assertEqual(self.scheduler.state()['backedUpGeneration'], 0)

    def test_disable_stops_new_starts_but_records_already_running_backup(self):
        self.enable_and_start()
        self.scheduler.configure(False, 300)
        self.backups.finish()
        self.scheduler._step()
        self.assertFalse(self.scheduler.state()['pending'])
        self.scheduler.changed()
        self.clock.advance(1000)
        self.assertIsNone(self.scheduler._step())
        self.assertEqual(self.backups.starts, 1)

    def test_start_failure_preserves_generation_and_error_across_restart(self):
        self.backups.fail_start = True
        self.scheduler.configure(True, 300)
        self.scheduler._step()
        self.assertTrue(self.scheduler.state()['pending'])
        self.assertEqual(self.backups.starts, 0)
        self.restart()
        self.scheduler._step()
        self.assertIn('Destination disconnected', self.scheduler.state()['lastError'])
        self.assertEqual(self.backups.starts, 0)
        self.clock.advance(300)
        self.backups.fail_start = False
        self.scheduler._step()
        self.assertEqual(self.backups.starts, 1)

    def test_crash_between_intent_and_receipt_does_not_guess_success(self):
        self.scheduler.configure(True, 300)
        self.scheduler.data.update(starting=True, runningGeneration=1, lastAttemptAt=self.clock())
        self.scheduler._persist()
        self.restart()
        self.scheduler._step()
        self.assertTrue(self.scheduler.state()['pending'])
        self.assertEqual(self.scheduler.state()['backedUpGeneration'], 0)
        self.assertEqual(self.backups.starts, 0)
        self.assertIn('interrupted', self.scheduler.state()['lastError'])

    def test_completion_acknowledgement_failure_cannot_erase_pending_state(self):
        self.enable_and_start()
        self.backups.finish()
        with patch('backup_scheduler.os.replace', side_effect=OSError('Disk full')):
            with self.assertRaisesRegex(OSError, 'Disk full'):
                self.scheduler._step()
        self.assertTrue(self.scheduler.state()['pending'])
        self.assertEqual(self.scheduler.state()['backedUpGeneration'], 0)
        self.assertFalse(self.scheduler.state()['progressDurable'])
        self.scheduler._step()
        self.assertFalse(self.scheduler.state()['pending'])
        self.assertEqual(self.backups.starts, 1)
        self.assertEqual(self.scheduler.path.stat().st_mode & 0o777, 0o600)

    def test_corrupted_schedule_disables_automation_without_locking_out_workspace(self):
        self.scheduler.configure(True, 300)
        self.scheduler.close()
        original = b'{broken\noriginal settings bytes'
        self.scheduler.path.write_bytes(original)
        self.restart()
        state = self.scheduler.state()
        self.assertFalse(state['enabled'])
        self.assertTrue(state['recoveryRequired'])
        self.assertTrue(state['pending'])
        self.assertEqual(state['backedUpGeneration'], 0)
        self.assertFalse(state['progressDurable'])
        self.assertIn('original settings are preserved', state['error'])
        self.scheduler.changed()
        self.assertIsNone(self.scheduler._step())
        self.assertEqual(self.backups.starts, 0)
        self.assertEqual(self.scheduler.path.read_bytes(), original)
        # The engine remains available even though its optional schedule broke.
        self.assertTrue(self.engine.create('Still available')['id'])

    def test_explicit_repair_archives_corrupt_bytes_and_requires_new_backup(self):
        self.scheduler.path.write_bytes(b'not a schedule')
        self.restart()
        self.scheduler.configure(True, 300)
        state = self.scheduler.state()
        self.assertFalse(state['recoveryRequired'])
        self.assertTrue(state['pending'])
        self.assertEqual(state['backedUpGeneration'], 0)
        self.assertEqual(Path(state['recoveryArchive']).read_bytes(), b'not a schedule')
        self.assertTrue(json.loads(self.scheduler.path.read_text())['enabled'])
        self.scheduler._step()
        self.backups.finish()
        self.scheduler._step()
        self.assertFalse(self.scheduler.state()['pending'])
        self.restart()
        self.assertFalse(self.scheduler.state()['recoveryRequired'])
        self.assertFalse(self.scheduler.state()['pending'])

    def test_symlink_schedule_is_preserved_without_reading_or_changing_target(self):
        external = Path(self.temp.name) / 'external'
        external.write_text('Private content outside scheduler')
        self.scheduler.path.symlink_to(external)
        self.restart()
        self.assertTrue(self.scheduler.state()['recoveryRequired'])
        self.assertFalse(self.scheduler.state()['enabled'])
        self.assertTrue(self.scheduler.path.is_symlink())
        self.scheduler.configure(False, 300)
        self.assertTrue(Path(self.scheduler.state()['recoveryArchive']).is_symlink())
        self.assertFalse(self.scheduler.path.is_symlink())
        self.assertEqual(external.read_text(), 'Private content outside scheduler')
        self.assertTrue(self.scheduler.state()['pending'])

    def test_failed_repair_retains_corrupt_archive_and_unknown_pending_across_restart(self):
        self.scheduler.path.write_bytes(b'preserve despite failure')
        self.restart()
        with patch.object(self.scheduler, '_persist', side_effect=OSError('Disk full')):
            with self.assertRaisesRegex(OSError, 'Disk full'):
                self.scheduler.configure(True, 300)
        self.assertTrue(self.scheduler.state()['recoveryRequired'])
        self.assertFalse(self.scheduler.state()['enabled'])
        self.assertTrue(self.scheduler.state()['pending'])
        archives = list(self.scheduler.root.glob('schedule.invalid-*.json'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), b'preserve despite failure')
        self.assertFalse(self.scheduler.path.exists())
        self.restart()
        self.assertTrue(self.scheduler.state()['recoveryRequired'])
        self.assertTrue(self.scheduler.state()['pending'])
        self.assertFalse(self.scheduler.state()['enabled'])
        self.scheduler.configure(False, 300)
        self.assertFalse(self.scheduler.state()['recoveryRequired'])
        self.assertTrue(self.scheduler.state()['pending'])

    def test_invalid_pending_generation_never_becomes_acknowledged(self):
        value = self.scheduler._empty()
        value.update(enabled=True, dirtyGeneration=2, backedUpGeneration=99)
        self.scheduler.path.write_text(json.dumps(value))
        self.restart()
        self.assertTrue(self.scheduler.state()['recoveryRequired'])
        self.assertFalse(self.scheduler.state()['enabled'])
        self.assertTrue(self.scheduler.state()['pending'])
        self.assertEqual(self.scheduler.state()['backedUpGeneration'], 0)

    def test_real_scheduler_thread_sleeps_when_idle_and_closes_without_orphan(self):
        self.scheduler.close()
        self.scheduler = BackupScheduler(self.engine, self.backups, poll_seconds=1)
        worker = self.scheduler.thread
        self.assertTrue(worker.is_alive())
        self.assertEqual(self.backups.starts, 0)
        self.scheduler.configure(True, 60)
        deadline = time.monotonic() + 2
        while self.backups.starts == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.backups.starts, 1)
        self.scheduler.close()
        self.assertFalse(worker.is_alive())
        # The scheduler does not own/cancel the backup service's worker.
        self.assertTrue(self.backups.worker.is_alive())


if __name__ == '__main__':
    unittest.main()
