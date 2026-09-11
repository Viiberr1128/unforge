"""Crash/restart boundaries use temporary homes and a fake local CLI only."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from agent_jobs import AgentJobs
from engine import Engine, Problem
from operations import Operations


class AgentDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.engine = Engine(self.base / 'home')
        self.project = self.engine.create('Durable garden')
        self.pid = self.project['id']
        self.fake = self.base / 'fake-codex'
        self.fake.write_text('#!' + sys.executable + '\n'
                             'from pathlib import Path\n'
                             'import sys\n'
                             'request = sys.stdin.read()\n'
                             'print("A preserved local log", flush=True)\n'
                             'Path("README.md").write_text("A durable proposal\\n")\n')
        self.fake.chmod(0o700)
        self.ledger = Operations(self.engine.home)
        self.jobs = AgentJobs(self.engine, executable=str(self.fake), operations=self.ledger)

    def tearDown(self):
        self.jobs.close()
        self.ledger.close()
        self.temp.cleanup()

    def finish(self, request='change', operation='one'):
        started = self.jobs.start(self.pid, request, operation)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            job = self.jobs.get(started['id'])
            if job['status'] != 'running':
                self.assertEqual(job['status'], 'completed', job)
                return job
            time.sleep(0.02)
        self.fail('The fake local process did not complete')

    def restart(self):
        self.jobs.close()
        self.ledger.close()
        self.ledger = Operations(self.engine.home)
        self.jobs = AgentJobs(self.engine, executable=str(self.base / 'absent'), operations=self.ledger)

    def test_completed_proposal_survives_restart_and_applies_without_another_cli(self):
        before = self.finish()
        self.assertEqual(self.jobs.desktop_work(), 0)
        self.restart()
        with patch('agent_jobs.subprocess.Popen', side_effect=AssertionError('Must not rerun CLI')):
            after = self.jobs.start(self.pid, 'change', 'one')
        self.assertEqual(after['id'], before['id'])
        for key in ('diff', 'output', 'request', 'baseVersion', 'changedFiles'):
            self.assertEqual(after[key], before[key])
        self.assertTrue(after['durable'])
        self.assertTrue(after['proposalAvailable'])
        self.assertTrue(after['recovered'])
        self.jobs.apply(after['id'])
        self.assertEqual((self.engine.root(self.pid) / 'README.md').read_text(), 'A durable proposal\n')
        self.restart()
        self.assertEqual(self.jobs.get(after['id'])['status'], 'applied')
        self.assertFalse(self.jobs.get(after['id'])['proposalAvailable'])
        self.assertEqual(self.ledger.overview()['usedToday'], 1)

    def test_restart_preserves_stale_proposal_but_does_not_overwrite_changed_source(self):
        before = self.finish()
        self.engine.edit(self.pid, 'README.md', 'New human decision')
        self.restart()
        with self.assertRaisesRegex(Problem, 'Save your current changes before merging'):
            self.jobs.apply(before['id'])
        self.assertEqual(self.jobs.get(before['id'])['diff'], before['diff'])
        self.assertEqual((self.engine.root(self.pid) / 'README.md').read_text(), 'New human decision')

    def test_interrupted_record_retains_log_and_replay_is_unknown_without_execution(self):
        before = self.finish()
        job = self.jobs._jobs[before['id']]
        job.update(status='running', finishedAt=None, diff='', changedFiles=[], output='Last durable progress')
        self.jobs._persist(job)
        # Simulate an execution owner disappearing before its terminal write.
        # No real external child is left behind by this test.
        self.jobs._owner.close()
        self.jobs._owner = None
        self.jobs._jobs.clear()
        self.restart()
        with patch('agent_jobs.subprocess.Popen', side_effect=AssertionError('Must not rerun CLI')):
            saved = self.jobs.start(self.pid, 'change', 'one')
        self.assertEqual(saved['status'], 'unknown')
        self.assertEqual(saved['output'], 'Last durable progress')
        self.assertFalse(saved['proposalAvailable'])
        self.assertEqual(self.ledger.overview()['usedToday'], 1)

    def test_partial_apply_receipt_recovers_applied_state_after_crash(self):
        before = self.finish()
        self.jobs.apply(before['id'])
        job = self.jobs._jobs[before['id']]
        job['status'] = 'applying'
        self.jobs._persist(job)
        self.restart()
        saved = self.jobs.get(before['id'])
        self.assertEqual(saved['status'], 'applied')
        self.assertFalse(saved['proposalAvailable'])
        with self.assertRaisesRegex(Problem, 'unapplied'):
            self.jobs.apply(before['id'])

    def test_partial_apply_without_source_record_is_unknown_not_retried(self):
        before = self.finish()
        self.jobs._jobs[before['id']]['status'] = 'applying'
        self.jobs._persist(self.jobs._jobs[before['id']])
        self.restart()
        self.assertEqual(self.jobs.get(before['id'])['status'], 'unknown')
        self.assertEqual((self.engine.root(self.pid) / 'README.md').read_text(), '# Durable garden\n\n\n')

    def test_retention_prunes_applied_record_but_never_unaccepted_proposal(self):
        first = self.finish()
        self.jobs.apply(first['id'])
        self.engine.save(self.pid, 'Accept first')
        second = self.finish('next', 'two')
        # Fake CLI writes the same contents; the second result is an empty
        # completed attempt, which is eligible for retention too.
        with patch('agent_jobs.MAX_SAVED_JOBS', 2):
            self.jobs._make_room()
        self.assertFalse(self.jobs._record_path(first['id']).exists())
        self.assertTrue(self.jobs._record_path(second['id']).exists())
        self.assertEqual(self.ledger.get('one')['state'], 'succeeded')

    def test_full_pending_history_blocks_new_work_without_discard_or_charge(self):
        before = self.finish()
        with patch('agent_jobs.MAX_SAVED_JOBS', 1):
            with self.assertRaisesRegex(Problem, 'history is full'):
                self.jobs.start(self.pid, 'next', 'two')
        self.assertTrue(self.jobs._record_path(before['id']).exists())
        self.assertEqual(self.ledger.overview()['usedToday'], 1)

    def test_more_than_32_saved_records_does_not_require_restart(self):
        first = self.finish()
        for _ in range(33):
            record = copy.deepcopy({key: value for key, value in first.items() if not key.startswith('_')})
            record['id'] = uuid.uuid4().hex
            record.pop('operationId', None)
            self.jobs._jobs[record['id']] = record
            self.jobs._persist(record)
        self.assertEqual(self.finish('next', 'two')['status'], 'completed')
        self.assertEqual(len(self.jobs._jobs), 35)

    def test_atomic_persistence_failure_keeps_previous_artifact(self):
        before = self.finish()
        path = self.jobs._record_path(before['id'])
        original = path.read_bytes()
        job = self.jobs._jobs[before['id']]
        job['output'] = 'An unacknowledged update'
        with patch('agent_jobs.os.replace', side_effect=OSError('Disk full')):
            with self.assertRaisesRegex(OSError, 'Disk full'):
                self.jobs._persist(job)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(path.parent.glob('.write-*')))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_record_is_reported_and_does_not_hide_healthy_job(self):
        before = self.finish()
        corrupted = self.jobs._store / (uuid.uuid4().hex + '.json')
        corrupted.write_text('{broken')
        self.restart()
        self.assertEqual(self.jobs.get(before['id'])['status'], 'completed')
        self.assertEqual(len(self.jobs.status()['historyErrors']), 1)
        self.assertTrue(corrupted.exists())

    def test_record_symlink_is_not_followed(self):
        before = self.finish()
        outside = self.base / 'private-file'
        outside.write_text('Do not parse or overwrite me')
        bad = self.jobs._store / (uuid.uuid4().hex + '.json')
        bad.symlink_to(outside)
        self.restart()
        self.assertEqual(self.jobs.get(before['id'])['status'], 'completed')
        self.assertEqual(len(self.jobs.status()['historyErrors']), 1)
        self.assertEqual(outside.read_text(), 'Do not parse or overwrite me')

    def test_well_formed_but_changed_artifact_is_rejected_by_checksum(self):
        before = self.finish()
        path = self.jobs._record_path(before['id'])
        envelope = json.loads(path.read_text())
        envelope['job']['diff'] += '\nAn accidental edit\n'
        path.write_text(json.dumps(envelope))
        self.restart()
        self.assertEqual(self.jobs.get(before['id'])['status'], 'archived')
        self.assertIn('checksum', self.jobs.status()['historyErrors'][0]['error'])

    def test_completed_records_release_snapshot_memory_and_stale_writes_are_cleaned(self):
        before = self.finish()
        self.assertNotIn('_baseline', self.jobs._jobs[before['id']])
        self.assertNotIn('_temporary', self.jobs._jobs[before['id']])
        orphan = self.jobs._store / ('.write-' + uuid.uuid4().hex + '-unfinished')
        orphan.write_text('Unpublished bytes')
        self.restart()
        self.assertFalse(orphan.exists())

    def test_live_owner_record_is_not_mislabeled_as_interrupted(self):
        before = self.finish()
        record = self.jobs._jobs[before['id']]
        record['status'] = 'running'
        self.jobs._persist(record)
        other = AgentJobs(self.engine)
        try:
            self.assertEqual(other.get(before['id'])['status'], 'running')
            with self.assertRaisesRegex(Problem, 'another running'):
                other.cancel(before['id'])
            self.assertEqual(self.jobs._jobs[before['id']]['status'], 'running')
        finally:
            record['status'] = 'completed'
            self.jobs._persist(record)
            other.close()


if __name__ == '__main__':
    unittest.main()
