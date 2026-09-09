"""Corrupt optional backup metadata must not lock users out of their projects."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from backups import Backups, atomic
from backup_scheduler import BackupScheduler
from engine import Engine, Problem


class BackupMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.engine = Engine(self.root / 'workspace')
        self.service = Backups(self.engine, executable='/unused-restic')
        self.password = 'original passphrase that must survive repair'
        self.destinations = [{'kind': 'folder', 'path': str(self.root / 'copies')}]
        self.service.configure(self.destinations, self.password)

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def reopen(self):
        self.service.close()
        self.service = Backups(self.engine, executable='/unused-restic')
        return self.service

    def test_invalid_receipts_preserve_bytes_and_do_not_prevent_valid_history_loading(self):
        invalid = [[], None, {'id': [], 'state': 'completed'},
                   {'id': 'x', 'state': 'completed'}, {'id': 'a' * 32, 'state': []},
                   {'id': 'a' * 32, 'state': 'completed', 'destinations': [None]}]
        originals = {}
        for value in invalid:
            path = self.service.root / ('job-' + uuid.uuid4().hex + '.json')
            path.write_text(json.dumps(value)); originals[path] = path.read_bytes()
        valid = {'id': 'b' * 32, 'state': 'completed'}  # Valid legacy sparse receipt.
        atomic(self.service.root / ('job-' + valid['id'] + '.json'), valid)
        service = self.reopen()
        state = service.state()
        self.assertEqual(len(state['jobs']), 1)
        self.assertEqual(state['jobs'][0]['id'], valid['id'])
        self.assertEqual(len(state['historyErrors']), len(invalid))
        for path, original in originals.items(): self.assertEqual(path.read_bytes(), original)
        self.assertNotIn('"id": []', json.dumps(state['historyErrors']))

    def test_corrupt_settings_are_visible_and_scheduler_cannot_start(self):
        original = b'{ broken settings containing PRIVATE ORIGINAL BYTES'
        self.service.config_path.write_bytes(original)
        service = self.reopen()
        state = service.state()
        self.assertFalse(state['configured']); self.assertTrue(state['settingsRecoveryRequired'])
        self.assertTrue(state['keyConfigured']); self.assertEqual(state['destinations'], [])
        self.assertNotIn('PRIVATE ORIGINAL', json.dumps(state))
        with self.assertRaises(Problem): service.start()
        scheduler = BackupScheduler(self.engine, service, start_thread=False)
        try:
            with self.assertRaises(Problem): scheduler.configure(True, 300)
        finally: scheduler.close()
        self.assertEqual(service.config_path.read_bytes(), original)

    def test_explicit_repair_archives_settings_and_keeps_existing_key(self):
        original = b'[ malformed settings'
        self.service.config_path.write_bytes(original)
        key_before = self.service.key_path.read_bytes()
        result = self.service.configure(self.destinations)
        self.assertTrue(result['configured']); self.assertFalse(result['settingsRecoveryRequired'])
        archives = list(self.service.root.glob('settings.invalid-*.json'))
        self.assertEqual(len(archives), 1); self.assertEqual(archives[0].read_bytes(), original)
        self.assertEqual(self.service.key_path.read_bytes(), key_before)
        self.assertEqual(self.reopen().settings()['destinations'][0]['path'], self.destinations[0]['path'])

    def test_failed_repair_after_archive_stays_disabled_on_restart(self):
        original = b'null'
        self.service.config_path.write_bytes(original)
        old_atomic = atomic
        def fail_settings(path, value):
            if Path(path) == self.service.config_path: raise OSError('simulated disk failure')
            return old_atomic(path, value)
        with patch('backups.atomic', side_effect=fail_settings):
            with self.assertRaises(OSError): self.service.configure(self.destinations)
        self.assertFalse(self.service.config_path.exists())
        self.assertTrue(self.reopen().state()['settingsRecoveryRequired'])
        self.assertEqual(next(self.service.root.glob('settings.invalid-*.json')).read_bytes(), original)
        self.assertTrue(self.service.configure(self.destinations)['configured'])

    def test_wrong_key_during_repair_changes_neither_original_key_nor_bad_settings(self):
        original = b'not JSON'
        self.service.config_path.write_bytes(original)
        with self.assertRaisesRegex(Problem, 'existing key'):
            self.service.configure(self.destinations, 'different passphrase must not overwrite')
        self.assertEqual(self.service.key_path.read_text(), self.password)
        self.assertEqual(self.service.config_path.read_bytes(), original)
        self.assertFalse(list(self.service.root.glob('settings.invalid-*.json')))

    def test_symlink_settings_archives_link_without_reading_or_changing_target(self):
        target = self.root / 'private-original'; target.write_bytes(b'private target contents')
        self.service.config_path.unlink(); self.service.config_path.symlink_to(target)
        self.assertTrue(self.service.state()['settingsRecoveryRequired'])
        result = self.service.configure(self.destinations)
        self.assertTrue(result['configured'])
        archive = next(self.service.root.glob('settings.invalid-*.json'))
        self.assertTrue(archive.is_symlink()); self.assertEqual(archive.readlink(), target)
        self.assertEqual(target.read_bytes(), b'private target contents')

    def test_schema_errors_and_nonregular_receipts_are_isolated(self):
        for value in ([], None, {'configured': True, 'destinations': None},
                      {'configured': True, 'destinations': [{'kind': 'folder', 'path': []}]},
                      {'configured': True, 'destinations': [{'kind': 'folder', 'path': 'relative'}]}):
            with self.subTest(value=value):
                self.service.config_path.write_text(json.dumps(value))
                self.assertTrue(self.service.state()['settingsRecoveryRequired'])
                with self.assertRaises(Problem): self.service.settings()
        invalid = self.service.root / ('job-' + 'd' * 32 + '.json'); invalid.mkdir()
        self.assertTrue(self.reopen().state()['historyErrors'])

    def test_interrupted_receipt_remains_visible_if_restart_cannot_persist_status(self):
        job = {'id': 'c' * 32, 'state': 'running', 'createdAt': '2026-09-09'}
        atomic(self.service.root / ('job-' + job['id'] + '.json'), job)
        with patch('backups.atomic', side_effect=OSError('simulated unwritable receipt')):
            service = self.reopen()
        self.assertEqual(service.jobs[job['id']]['state'], 'interrupted')
        self.assertTrue(service.state()['historyErrors']); self.assertIsNone(service.worker)

    def test_deeply_nested_json_cannot_prevent_access(self):
        nested = '[' * 2000 + '0' + ']' * 2000
        self.service.config_path.write_text(nested)
        receipt = self.service.root / ('job-' + 'e' * 32 + '.json')
        receipt.write_text(nested)
        service = self.reopen()
        self.assertTrue(service.state()['settingsRecoveryRequired'])
        self.assertTrue(service.state()['historyErrors'])
        self.assertEqual(receipt.read_text(), nested)

    def test_existing_key_link_never_becomes_a_password_reset(self):
        target = self.root / 'secret'; target.write_text(self.password)
        self.service.key_path.unlink(); self.service.key_path.symlink_to(target)
        self.assertFalse(self.service.state()['keyConfigured'])
        with self.assertRaises(Problem): self.service.configure(self.destinations)
        self.assertEqual(target.read_text(), self.password)


if __name__ == '__main__': unittest.main()
