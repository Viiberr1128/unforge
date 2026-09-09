"""Backup disk preflights use mocked capacity; no large files or Restic processes."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from backups import Backups, MAX_STORAGE_RESERVE, MIN_STORAGE_RESERVE, STORAGE_METADATA_ALLOWANCE, estimated_workspace_storage, storage_guard
from engine import Problem

GIB = 1024**3


def capacity(free, total=20 * GIB):
    return SimpleNamespace(total=total, used=max(0, total-free), free=free)


class StorageGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='unforge-storage-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.one = self.root / 'one'; self.one.mkdir()
        self.two = self.root / 'two'; self.two.mkdir()

    def test_requirements_on_one_volume_are_combined(self):
        with patch('backups.shutil.disk_usage', return_value=capacity(5 * GIB)):
            with self.assertRaisesRegex(Problem, 'Not enough free disk space'):
                storage_guard([(self.one, 3 * GIB, 'stage'), (self.two, 3 * GIB, 'destination')])

    def test_separate_volumes_keep_separate_budgets(self):
        original_stat = Path.stat
        def device(path, *args, **kwargs):
            if path in (self.one, self.two):
                return SimpleNamespace(st_dev=1 if path == self.one else 2)
            return original_stat(path, *args, **kwargs)
        with patch('backups.Path.stat', new=device), patch('backups.shutil.disk_usage', return_value=capacity(5 * GIB)):
            plans = storage_guard([(self.one, 3 * GIB, 'stage'), (self.two, 3 * GIB, 'destination')])
        self.assertEqual(len(plans), 2)
        self.assertEqual([item['requiredBytes'] for item in plans], [3 * GIB, 3 * GIB])

    def test_reserve_has_floor_percentage_and_large_volume_cap(self):
        for total, expected in [(2 * GIB, MIN_STORAGE_RESERVE), (100 * GIB, 5 * GIB), (1024 * GIB, MAX_STORAGE_RESERVE)]:
            with patch('backups.shutil.disk_usage', return_value=capacity(total, total)):
                self.assertEqual(storage_guard([(self.one, 0, 'test')])[0]['reserveBytes'], expected)

    def test_unknown_capacity_fails_closed(self):
        with patch('backups.shutil.disk_usage', side_effect=OSError('unavailable')):
            with self.assertRaisesRegex(Problem, 'Could not verify free disk space'):
                storage_guard([(self.one, 1, 'backup')])

    def test_source_estimate_includes_allocation_and_manifest_overhead(self):
        listing = {'a': (1, 1, 1, 0), 'b': (1, 2, 4097, 0)}
        self.assertEqual(estimated_workspace_storage(listing), 4096 + 8192 + 2 * 512 + STORAGE_METADATA_ALLOWANCE)

    def service(self):
        workspace = self.root / 'workspace'; workspace.mkdir()
        (workspace / 'source.txt').write_text('source remains unchanged')
        service = Backups(SimpleNamespace(home=workspace, lock=threading.RLock()), executable='/unused/restic')
        self.addCleanup(service.close)
        service.configure([{'kind': 'folder', 'path': str(self.two)}], 'fictional-test-passphrase')
        return service

    def test_low_space_rejects_before_source_copy_and_preserves_old_points(self):
        service = self.service()
        previous = self.two / 'previous.ufbackup'; previous.mkdir()
        (previous / 'keep.txt').write_text('previous good recovery point')
        job = {'id': 'a'*32, 'kind': 'backup', 'state': 'running', 'createdAt': '2026-09-09T00:00:00Z', 'destinations': []}
        with patch('backups.shutil.disk_usage', return_value=capacity(100)), patch.object(service, '_copy_workspace') as copying, patch.object(service, '_command') as command:
            service._backup(job)
            copying.assert_not_called()
            command.assert_not_called()
        self.assertEqual(job['state'], 'failed')
        self.assertIn('Not enough free disk space', job['error'])
        self.assertEqual((previous / 'keep.txt').read_text(), 'previous good recovery point')
        self.assertEqual(list(self.two.glob('.pending-*')), [])
        self.assertEqual(list(service.root.glob('snapshot-*')), [])

    def test_restore_checks_uncompressed_size_before_restic_restore(self):
        service = self.service()
        source = self.one / 'source.ufbackup'; source.mkdir()
        (source / 'repository').mkdir()
        target = self.root / 'recovered'
        stats = json.dumps({'total_size': 4 * GIB, 'total_file_count': 100})
        with patch.object(service, 'verify_container'), patch.object(service, '_command', return_value=stats) as command, patch('backups.shutil.disk_usage', return_value=capacity(3 * GIB)):
            with self.assertRaisesRegex(Problem, 'Not enough free disk space'):
                service.restore(str(source), 'fictional-test-passphrase', str(target))
            self.assertEqual(command.call_count, 1)
            self.assertIn('stats', command.call_args.args)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob('.unforge-restore-*')), [])


if __name__ == '__main__':
    unittest.main()
