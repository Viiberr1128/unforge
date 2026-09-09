from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from operations import MAX_PAYLOAD, OperationError, Operations


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.ledger = Operations(self.home)

    def tearDown(self):
        self.ledger.close()
        self.temporary.cleanup()

    def test_practice_is_local_and_repeated_id_returns_same_receipt(self):
        payload = {'to': 'not-a-real-recipient', 'text': 'private-practice-text'}
        with patch('socket.socket', side_effect=AssertionError('Practice must never use the network')):
            first = self.ledger.practice('garden', 'email-1', 'email', payload)
            again = self.ledger.practice('garden', 'email-1', 'email', payload)
        self.assertEqual(first['result'], again['result'])
        self.assertEqual(again['state'], 'succeeded')
        self.assertTrue(again['replayed'])
        self.assertEqual(self.ledger.overview()['usedToday'], 1)
        self.assertNotIn(b'private-practice-text', self.ledger.path.read_bytes())
        self.assertEqual(self.ledger.path.stat().st_mode & 0o777, 0o600)

    def test_shared_daily_cap_is_atomic_across_projects_and_instances(self):
        self.ledger.configure(3, 1)
        second = Operations(self.home)
        try:
            def run(index):
                ledger = self.ledger if index % 2 else second
                try:
                    return ledger.practice('project-' + str(index), 'attempt-' + str(index), 'webhook', {'n': index})
                except OperationError as exc:
                    return str(exc)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(run, range(12)))
            self.assertEqual(sum(isinstance(item, dict) for item in results), 3)
            self.assertTrue(all(isinstance(item, dict) or 'allowance reached' in item for item in results))
            self.assertEqual(self.ledger.overview()['usedToday'], 3)
            self.assertEqual(second.overview()['remainingToday'], 0)
        finally:
            second.close()

    def test_concurrent_replay_reserves_once_and_payload_order_is_irrelevant(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(lambda _: self.ledger.practice('garden', 'same', 'payment', {'b': 2, 'a': 1}), range(10)))
        self.assertEqual(self.ledger.overview()['usedToday'], 1)
        final = self.ledger.practice('garden', 'same', 'payment', {'a': 1, 'b': 2})
        self.assertEqual(final['state'], 'succeeded')
        self.assertEqual(final['result']['receiptId'], 'same')
        self.assertEqual(sum(not record['replayed'] for record in records), 1)

    def test_reusing_id_for_different_intent_is_rejected(self):
        self.ledger.practice('garden', 'bound', 'email', {'body': 'one'})
        for project, kind, payload in [('other', 'email', {'body': 'one'}), ('garden', 'payment', {'body': 'one'}), ('garden', 'email', {'body': 'two'})]:
            with self.subTest(project=project, kind=kind, payload=payload):
                with self.assertRaisesRegex(OperationError, 'already bound'):
                    self.ledger.reserve(project, 'bound', kind, payload)
        self.assertEqual(self.ledger.overview()['usedToday'], 1)

    def test_failed_empty_unknown_sequence_pauses_until_explicit_resume(self):
        for index, outcome in enumerate(('failed', 'empty', 'unknown')):
            self.ledger.reserve('garden', str(index), 'agent', {'request': str(index)})
            self.ledger.finish(str(index), outcome, {'outcome': outcome})
        self.assertEqual(self.ledger.overview()['pausedProjects'], [{'projectId': 'garden', 'consecutiveFailures': 3}])
        with self.assertRaisesRegex(OperationError, 'paused'):
            self.ledger.reserve('garden', 'fourth', 'agent', {})
        reconciled = self.ledger.reconcile('2', 'succeeded', 'Checked the recorded output independently.')
        self.assertEqual(reconciled['state'], 'succeeded')
        self.assertEqual(self.ledger.overview()['usedToday'], 3)
        with self.assertRaisesRegex(OperationError, 'paused'):
            self.ledger.reserve('garden', 'fourth', 'agent', {})
        self.ledger.resume('garden')
        self.ledger.practice('garden', 'fourth', 'email', {})
        self.assertEqual(self.ledger.overview()['usedToday'], 4)
        self.assertFalse(self.ledger.overview()['pausedProjects'])

    def test_success_breaks_failure_streak_but_does_not_clear_existing_pause(self):
        for i, outcome in enumerate(('failed', 'succeeded', 'empty', 'failed')):
            self.ledger.reserve('garden', str(i), 'agent', {})
            self.ledger.finish(str(i), outcome, {})
        self.assertFalse(self.ledger.overview()['pausedProjects'])
        # A success already in flight must not silently resume a paused project.
        self.ledger.reserve('garden', 'late-success', 'agent', {})
        self.ledger.reserve('garden', 'third-failure', 'agent', {})
        self.ledger.finish('third-failure', 'failed', {})
        self.ledger.finish('late-success', 'succeeded', {})
        self.assertEqual(self.ledger.overview()['pausedProjects'], [{'projectId': 'garden', 'consecutiveFailures': 3}])

    def test_day_rollover_keeps_old_ids_and_never_refunds_attempts(self):
        self.ledger.configure(1, 1)
        with patch('operations.day', return_value='2026-01-01'):
            first = self.ledger.practice('garden', 'old', 'payment', {})
            self.assertEqual(self.ledger.overview()['remainingToday'], 0)
        with patch('operations.day', return_value='2026-01-02'):
            self.assertEqual(self.ledger.overview()['remainingToday'], 1)
            replay = self.ledger.practice('garden', 'old', 'payment', {})
            self.assertEqual(replay['result'], first['result'])
            self.assertEqual(self.ledger.overview()['usedToday'], 0)
            self.ledger.practice('other', 'new', 'payment', {})
            self.assertEqual(self.ledger.overview()['usedToday'], 1)

    def test_revision_prevents_stale_settings_and_zero_stops_new_work(self):
        updated = self.ledger.configure(0, 1)
        self.assertEqual(updated['revision'], 2)
        with self.assertRaisesRegex(OperationError, 'changed'):
            self.ledger.configure(100, 1)
        with self.assertRaisesRegex(OperationError, 'allowance reached'):
            self.ledger.practice('garden', 'none', 'email', {})
        for value in (True, -1, 1.5, '10', 100001):
            with self.assertRaises(OperationError):
                self.ledger.configure(value, 2)

    def test_lease_distinguishes_live_owner_and_abandoned_attempt_in_same_process(self):
        second = Operations(self.home)
        second.reserve('garden', 'abandoned', 'agent', {}, reference_id='job-1', initial_result={'baseVersion': 'abc'})
        self.assertEqual(self.ledger.get('abandoned')['state'], 'running')
        with self.assertRaisesRegex(OperationError, 'execution owner'):
            self.ledger.finish('abandoned', 'succeeded', {})
        second.close()
        recovered = self.ledger.by_reference('job-1')
        self.assertEqual(recovered['state'], 'unknown')
        self.assertEqual(recovered['result']['baseVersion'], 'abc')
        self.assertTrue(self.ledger.reserve('garden', 'abandoned', 'agent', {})['replayed'])
        self.assertEqual(self.ledger.overview()['usedToday'], 1)

    def test_real_process_crash_becomes_unknown_and_cannot_repeat(self):
        code = "from operations import Operations; import os, sys; ledger=Operations(sys.argv[1]); ledger.reserve('garden','crashed','agent',{'request':'once'}); os._exit(0)"
        subprocess.run([sys.executable, '-c', code, str(self.home)], cwd=Path(__file__).parent, check=True, timeout=10)
        record = self.ledger.get('crashed')
        self.assertEqual(record['state'], 'unknown')
        repeated = self.ledger.reserve('garden', 'crashed', 'agent', {'request': 'once'})
        self.assertTrue(repeated['replayed'])
        self.assertEqual(repeated['state'], 'unknown')
        with self.assertRaisesRegex(OperationError, 'same intent'):
            self.ledger.reserve('garden', 'different-id', 'agent', {'request': 'once'})
        with self.assertRaisesRegex(OperationError, 'unknown outcomes'):
            self.ledger.resume('garden')
        self.ledger.reconcile('crashed', 'failed', 'Checked that the interrupted local execution produced no result.')
        self.assertEqual(self.ledger.resume('garden')['paused'], False)
        self.assertEqual(self.ledger.reserve('garden', 'different-id', 'agent', {'request': 'once'})['state'], 'running')
        with self.assertRaisesRegex(OperationError, 'already recorded'):
            self.ledger.finish('crashed', 'failed', {})

    def test_finish_is_idempotent_but_cannot_rewrite_a_known_outcome(self):
        self.ledger.reserve('garden', 'one', 'agent', {})
        self.ledger.finish('one', 'failed', {'reason': 'empty'})
        self.assertTrue(self.ledger.finish('one', 'failed', {'reason': 'empty'})['replayed'])
        with self.assertRaisesRegex(OperationError, 'already recorded'):
            self.ledger.finish('one', 'succeeded', {})
        with self.assertRaisesRegex(OperationError, 'Only unknown'):
            self.ledger.reconcile('one', 'succeeded', 'Pretend success')

    def test_old_unknowns_remain_accessible_beyond_recent_history_with_bounded_pages(self):
        self.ledger.configure(1000, 1)
        for index in range(105):
            operation = 'unknown-' + str(index).zfill(3)
            self.ledger.reserve('project-' + str(index), operation, 'agent', {})
            self.ledger.finish(operation, 'unknown', {'message': 'Missing execution receipt'})
        for index in range(105):
            self.ledger.practice('healthy', 'later-' + str(index), 'email', {})
        overview = self.ledger.overview()
        self.assertEqual(len(overview['operations']), 100)
        self.assertTrue(all(item['state'] == 'succeeded' for item in overview['operations']))
        self.assertEqual(overview['unknownCount'], 105)
        self.assertEqual(len(overview['unknownOperations']), 100)
        self.assertEqual(overview['unknownOperations'][0]['operationId'], 'unknown-000')
        self.assertEqual(overview['unknownOperations'][-1]['operationId'], 'unknown-099')
        for item in overview['unknownOperations'][:5]:
            self.ledger.reconcile(item['operationId'], 'failed', 'Independently checked that this attempt did not complete.')
        next_page = self.ledger.overview()
        self.assertEqual(next_page['unknownCount'], 100)
        self.assertEqual(len(next_page['unknownOperations']), 100)
        self.assertEqual(next_page['unknownOperations'][0]['operationId'], 'unknown-005')
        self.assertEqual(next_page['unknownOperations'][-1]['operationId'], 'unknown-104')

    def test_invalid_payloads_and_identifiers_do_not_reserve(self):
        for operation in ('../escape', '', 'x' * 129, None, ['bad']):
            with self.assertRaises(OperationError):
                self.ledger.reserve('garden', operation, 'email', {})
        for payload in ([], {'bad': float('nan')}, {'huge': 'x' * MAX_PAYLOAD}):
            with self.assertRaises(OperationError):
                self.ledger.reserve('garden', 'invalid', 'email', payload)
        with self.assertRaises(OperationError):
            self.ledger.practice('garden', 'invalid', [], {})
        self.assertEqual(self.ledger.overview()['usedToday'], 0)

    def test_symlink_ledger_and_owner_directory_are_rejected(self):
        self.ledger.close()
        other = self.home / 'other.sqlite'
        self.ledger.path.rename(other)
        self.ledger.path.symlink_to(other)
        with self.assertRaisesRegex(OperationError, 'symbolic link'):
            Operations(self.home)
        self.ledger.path.unlink()
        other.rename(self.ledger.path)
        self.ledger.owners.rmdir()
        self.ledger.owners.symlink_to(self.home, target_is_directory=True)
        with self.assertRaisesRegex(OperationError, 'symbolic link'):
            Operations(self.home)


if __name__ == '__main__':
    unittest.main()
