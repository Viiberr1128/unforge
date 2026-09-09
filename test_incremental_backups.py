"""Temporary local vaults only; no cloud provider or user workspace operations."""
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from backups import Backups, atomic, digest, restic_path, stamp
from engine import Engine, Problem
from incremental_backups import IncrementalBackups, _lease


@unittest.skipUnless(restic_path(), 'Install pinned restic with scripts/fetch_restic.py')
class IncrementalBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.engine = Engine(self.root / 'home')
        self.project = self.engine.create('Incremental recovery fixture')
        self.source = self.engine.root(self.project['id'])
        self.password = 'a separately held incremental recovery passphrase'
        self.service = Backups(self.engine)
        self.destination = self.root / 'copies'
        self.service.configure([{'kind': 'folder', 'path': str(self.destination)}], self.password)
        self.incremental = IncrementalBackups(self.service)
        (self.source / 'personal.txt').write_text('first revision')

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def backup(self, expect='completed', point_id=None):
        job = {'id': point_id or uuid.uuid4().hex, 'kind': 'backup', 'state': 'running',
               'createdAt': stamp(), 'destinations': []}
        result = self.incremental.backup(job)
        self.assertEqual(result['state'], expect, result)
        return result

    def default_backup(self, *, standalone=False):
        initial = self.service.start(standalone=standalone)
        self.service.worker.join(60)
        self.assertFalse(self.service.worker.is_alive(), 'Backup worker did not finish')
        result = self.service.jobs[initial['id']]
        self.assertEqual(result['state'], 'completed', result)
        return result

    def point(self, job):
        return Path(job['destinations'][0]['backupPath'])

    def recover(self, point, name='recovered'):
        result = self.incremental.restore(str(point), self.password, str(self.root / name))
        self.assertFalse(result['applicationsStarted'])
        return Path(result['path']) / self.project['id']

    def test_repeated_points_deduplicate_and_exact_older_snapshot_survives_restart(self):
        # Incompressible fixture makes full-copy regressions distinguishable from
        # a misleading all-zero fixture that compresses to almost nothing.
        payload = os.urandom(8 * 1024**2)
        (self.source / 'media.bin').write_bytes(payload)
        first = self.default_backup(); first_point = self.point(first)
        first_inventory = self.incremental.inspect(first_point)
        first_hashes = {item['path']: item['sha256'] for item in first_inventory['requiredFiles']}
        (self.source / 'personal.txt').write_text('second revision')
        second = self.default_backup(); second_point = self.point(second)
        self.assertGreater(first['addedBytes'], 7 * 1024**2)
        self.assertLess(second['addedBytes'], first['addedBytes'] // 5, second)
        self.assertGreater(second['reusedBytes'], 7 * 1024**2)
        self.assertNotEqual(first['snapshotId'], second['snapshotId'])
        # Compare every byte of the prior point and its shared dependencies.
        self.assertEqual(first_hashes, {item['path']: item['sha256'] for item in self.incremental.inspect(first_point)['requiredFiles']})
        older = self.recover(first_point, 'older')
        newer = self.recover(second_point, 'newer')
        self.assertEqual((older / 'personal.txt').read_text(), 'first revision')
        self.assertEqual((newer / 'personal.txt').read_text(), 'second revision')
        self.assertEqual((older / 'media.bin').read_bytes(), payload)
        # Another engine has no original key or private Restic repository.
        other = Backups(Engine(self.root / 'replacement'))
        try:
            replacement = IncrementalBackups(other)
            result = replacement.restore(str(first_point), self.password, str(self.root / 'fresh-mac'))
            self.assertEqual(result['snapshotId'], first['snapshotId'])
            self.assertFalse((Path(result['path']) / '.backups').exists())
            self.assertNotIn('personal.txt', (first_point / 'POINT.json').read_text())
        finally: other.close()
        self.incremental = IncrementalBackups(self.service)
        third = self.default_backup()
        self.assertEqual(third['repositoryId'], first['repositoryId'])
        self.assertLess(third['addedBytes'], first['addedBytes'] // 5)

    def test_default_worker_and_restore_dispatch_keep_standalone_v1_compatible(self):
        legacy = self.default_backup(standalone=True)
        legacy_point = self.point(legacy)
        self.assertEqual(legacy_point.suffix, '.ufbackup')
        (self.source / 'personal.txt').write_text('after switching to shared storage')
        current = self.default_backup()
        current_point = self.point(current)
        self.assertEqual(current_point.suffix, '.ufpoint')
        self.assertEqual(current['format'], 'incremental-v2')
        before = self.service.restore(str(legacy_point), self.password, str(self.root / 'legacy-restored'))
        after = self.service.restore(str(current_point), self.password, str(self.root / 'current-restored'))
        self.assertEqual((Path(before['path']) / self.project['id'] / 'personal.txt').read_text(), 'first revision')
        self.assertEqual((Path(after['path']) / self.project['id'] / 'personal.txt').read_text(), 'after switching to shared storage')
        self.assertFalse(before['applicationsStarted']); self.assertFalse(after['applicationsStarted'])

    def test_rollover_seals_whole_generation_and_old_new_points_restore(self):
        self.incremental = IncrementalBackups(self.service, max_points=1)
        first = self.backup(); first_point = self.point(first)
        old_root = self.incremental.root
        old_identity = (old_root / 'identity.json').read_bytes()
        old_snapshot = (old_root / 'repository/snapshots' / first['snapshotId']).read_bytes()
        (self.source / 'personal.txt').write_text('after vault rollover')
        second = self.backup(); second_point = self.point(second)
        self.assertNotEqual(first['repositoryId'], second['repositoryId'])
        self.assertNotEqual(first_point.parent.parent, second_point.parent.parent)
        self.assertTrue(second['vaultRollover']['sealedPreviousGeneration'])
        self.assertEqual((old_root / 'identity.json').read_bytes(), old_identity)
        self.assertEqual((old_root / 'repository/snapshots' / first['snapshotId']).read_bytes(), old_snapshot)
        self.assertTrue((old_root / 'SEALED.json').exists())
        self.assertEqual((self.recover(first_point, 'before-rollover') / 'personal.txt').read_text(), 'first revision')
        self.assertEqual((self.recover(second_point, 'after-rollover') / 'personal.txt').read_text(), 'after vault rollover')
        # Default limits on restart should retain this new generation, not fall
        # back to the legacy writer or misreport another full-seed rollover.
        self.incremental = IncrementalBackups(self.service)
        third = self.backup()
        self.assertEqual(third['repositoryId'], second['repositoryId'])
        self.assertNotIn('vaultRollover', third)
        self.assertLess(third['destinations'][0]['vaultEntries'], 8000)

    def test_entry_threshold_rollover_and_interrupted_pointer_commit_resume_safely(self):
        self.incremental = IncrementalBackups(self.service, max_entries=1)
        first = self.backup(); old_root = self.incremental.root
        original_atomic = atomic
        pointer = self.incremental.pointer
        def fail_pointer(path, value):
            if Path(path) == pointer and value.get('generation') != 'legacy':
                raise OSError('simulated power loss before pointer promotion')
            return original_atomic(path, value)
        with patch('incremental_backups.atomic', side_effect=fail_pointer):
            failed = self.backup('failed')
        self.assertIn('power loss', failed['error'])
        self.assertEqual(json.loads(pointer.read_text())['generation'], 'legacy')
        seal = json.loads((old_root / 'SEALED.json').read_text())
        next_root = self.incremental.generations / seal['nextGeneration']
        next_identity = (next_root / 'identity.json').read_bytes()
        self.assertFalse((next_root / 'repository').exists())
        self.assertEqual((self.recover(self.point(first), 'interrupted-rollover') / 'personal.txt').read_text(), 'first revision')
        self.incremental = IncrementalBackups(self.service)
        resumed = self.backup()
        self.assertEqual(json.loads(pointer.read_text())['generation'], seal['nextGeneration'])
        self.assertEqual((next_root / 'identity.json').read_bytes(), next_identity)
        self.assertNotEqual(resumed['repositoryId'], first['repositoryId'])
        self.assertEqual((self.recover(self.point(resumed), 'resumed-rollover') / 'personal.txt').read_text(), 'first revision')

    def test_pointer_failure_after_commit_restarts_consistent_and_oversize_publication_fails(self):
        self.incremental = IncrementalBackups(self.service, max_points=1)
        first = self.backup()
        original_atomic = atomic
        pointer = self.incremental.pointer
        def fail_after_pointer(path, value):
            original_atomic(path, value)
            if Path(path) == pointer and value.get('generation') != 'legacy':
                raise OSError('simulated failure after pointer fsync')
        with patch('incremental_backups.atomic', side_effect=fail_after_pointer):
            self.backup('failed')
        selected = json.loads(pointer.read_text())['generation']
        self.assertNotEqual(selected, 'legacy')
        self.incremental = IncrementalBackups(self.service)
        resumed = self.backup()
        self.assertEqual(self.incremental.generation, selected)
        self.assertNotEqual(first['repositoryId'], resumed['repositoryId'])
        with patch('incremental_backups.MAX_VAULT_ENTRIES', 1):
            oversized = self.backup('failed')
        self.assertIn('entry limit', oversized['error'])
        vault = Path(resumed['destinations'][0]['vaultPath'])
        self.assertFalse((vault / 'points' / (oversized['id'] + '.ufpoint')).exists())
        self.assertEqual((self.recover(self.point(first), 'old-intact') / 'personal.txt').read_text(), 'first revision')

    def test_interrupted_publication_preserves_previous_point_and_retry_reuses_objects(self):
        first = self.backup(); point = self.point(first)
        (self.source / 'more.bin').write_bytes(os.urandom(2 * 1024**2))
        original_install = self.incremental._install
        copied = []
        def interrupt(source, target, entry):
            if entry['path'].startswith('snapshots/'):
                raise Problem('simulated process interruption before snapshot publication')
            result = original_install(source, target, entry)
            if result: copied.append(str(target))
            return result
        with patch.object(self.incremental, '_install', side_effect=interrupt):
            failed = self.backup('failed')
        self.assertTrue(copied)
        vault = Path(first['destinations'][0]['vaultPath'])
        self.assertFalse((vault / 'points' / (failed['id'] + '.ufpoint')).exists())
        self.assertEqual((self.recover(point) / 'personal.txt').read_text(), 'first revision')
        complete = self.backup()
        self.assertGreater(complete['reusedBytes'], 2 * 1024**2)
        self.assertTrue(self.point(complete).is_dir())
        self.assertFalse(list(vault.rglob('.incoming-*')))
        self.assertFalse(list(vault.rglob('.pending-point-*')))

    def test_wrong_key_missing_pack_damage_and_point_tampering_fail_closed(self):
        job = self.backup(); point = self.point(job)
        with self.assertRaises(Problem):
            self.incremental.restore(str(point), 'wrong secret', str(self.root / 'wrong'))
        self.assertFalse((self.root / 'wrong').exists())
        inspection = self.incremental.inspect(point)
        pack = next(item for item in inspection['files'] if item['path'].startswith('data/'))
        path = Path(inspection['repositoryPath']) / pack['path']; saved = path.read_bytes()
        path.unlink()
        with self.assertRaises(Problem): self.recover(point, 'missing')
        path.write_bytes(saved)
        damaged = bytearray(saved); damaged[0] ^= 1; path.write_bytes(damaged)
        with self.assertRaises(Problem): self.recover(point, 'damaged')
        path.write_bytes(saved)
        descriptor = json.loads((point / 'POINT.json').read_text())
        descriptor['workspaceManifestSha256'] = 'a' * 64
        atomic(point / 'POINT.json', descriptor)
        with self.assertRaises(Problem): self.recover(point, 'tampered')
        self.assertFalse((self.root / 'tampered').exists())

    def test_duplicate_local_writer_identity_collision_and_object_collision_do_not_replace(self):
        with _lease(self.incremental.root / 'writer.lock'):
            failed = self.backup('failed')
        self.assertIn('writer', failed['error'])
        first = self.backup(); point = self.point(first)
        descriptor_bytes = (point / 'POINT.json').read_bytes()
        collision = self.backup('failed', point_id=first['id'])
        self.assertIn('already exists', collision['error'])
        self.assertEqual((point / 'POINT.json').read_bytes(), descriptor_bytes)
        identity_path = self.incremental.root / 'identity.json'
        identity = json.loads(identity_path.read_text())
        atomic(identity_path, {'schema': 1, 'writerId': 'b' * 32})
        self.backup('failed')
        atomic(identity_path, identity)
        self.assertEqual((self.recover(point) / 'personal.txt').read_text(), 'first revision')
        inspection = self.incremental.inspect(point)
        pack = next(entry for entry in inspection['files'] if entry['path'].startswith('data/'))
        path = Path(inspection['repositoryPath']) / pack['path']
        path.write_bytes(b'collision must not be overwritten')
        self.backup('failed')
        self.assertEqual(path.read_bytes(), b'collision must not be overwritten')

    def test_private_wrong_key_path_escape_symlink_and_unrelated_future_index(self):
        first = self.backup(); point = self.point(first)
        self.service.key_path.write_text('wrong private key')
        failed = self.backup('failed')
        self.assertIn('Backup tool failed', failed['error'])
        self.service.key_path.write_text(self.password)
        descriptor = json.loads((point / 'POINT.json').read_text())
        modified = json.loads(json.dumps(descriptor))
        modified['files'][0]['path'] = '../escape'
        atomic(point / 'POINT.json', modified)
        with self.assertRaises(Problem): self.incremental.inspect(point)
        atomic(point / 'POINT.json', descriptor)
        # An unrelated, corrupt later index must never be read by old-point restore.
        repository = Path(self.incremental.inspect(point)['repositoryPath'])
        extra = repository / 'index' / ('a' * 64); extra.write_bytes(b'not an index')
        self.assertEqual((self.recover(point) / 'personal.txt').read_text(), 'first revision')
        alias = self.root / 'alias'; alias.symlink_to(point, target_is_directory=True)
        with self.assertRaises(Problem): self.incremental.inspect(alias)
        output_alias = self.root / 'redirect'; output_alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(Problem): self.incremental.restore(str(point), self.password, str(output_alias / 'new'))

    def test_same_point_id_is_cryptographically_bound_and_destination_lock_excludes_writers(self):
        first = self.backup(); point = self.point(first)
        repository_id = first['repositoryId']
        with _lease(self.destination / ('.unforge-writer-' + repository_id + '.lock')):
            failed = self.backup('failed')
        self.assertIn('writer', failed['error'])
        (self.source / 'personal.txt').write_text('second revision')
        second = self.backup(); second_point = self.point(second)
        # Rename/copy a valid descriptor to impersonate a different point: the
        # encrypted workspace marker must still reject it even with honest hashes.
        value = json.loads((second_point / 'POINT.json').read_text())
        value['pointId'] = first['id']
        atomic(point / 'POINT.json', value)
        with self.assertRaises(Problem): self.recover(point, 'impersonated')
        self.assertFalse((self.root / 'impersonated').exists())


class IncrementalObjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.incremental = IncrementalBackups(SimpleNamespace(root=self.root, closed=threading.Event()))
        self.source = self.root / 'ciphertext'; self.source.write_bytes(b'encrypted fixture bytes')
        self.entry = {'path': 'config', 'bytes': self.source.stat().st_size, 'sha256': digest(self.source)}

    def tearDown(self): self.tmp.cleanup()

    def test_damaged_missing_or_escaping_generation_pointer_never_selects_another_writer(self):
        for value in ({'schema': 1, 'generation': '../outside'}, {'schema': 1, 'generation': []}, {'schema': 7, 'generation': 'legacy'}):
            atomic(self.incremental.pointer, value)
            with self.assertRaises(Problem): self.incremental._select_generation()
        self.incremental.pointer.write_text('not JSON')
        with self.assertRaises(Problem): self.incremental._select_generation()
        self.incremental.pointer.unlink()
        self.incremental.generations.mkdir()
        with self.assertRaises(Problem): self.incremental._select_generation()
        atomic(self.incremental.pointer, {'schema': 1, 'generation': 'a' * 32})
        with self.assertRaises(Problem): self.incremental._select_generation()
        self.assertFalse((self.incremental.generations / ('a' * 32)).exists())

    def test_stable_writer_lock_excludes_generations_and_thresholds_are_bounded(self):
        with _lease(self.root / 'incremental-writer.lock'):
            with self.assertRaisesRegex(Problem, 'writer'):
                with self.incremental._writer(): self.fail('Duplicate writer entered')
        for kwargs in ({'max_points': 0}, {'max_points': 501}, {'max_entries': 4001}, {'max_entries': True}):
            with self.assertRaises(Problem): IncrementalBackups(self.incremental.service, **kwargs)

    def test_view_cross_volume_fallback_copies_and_checks_capacity(self):
        repository = self.root / 'original'; repository.mkdir()
        (repository / 'config').write_bytes(self.source.read_bytes())
        original_link = os.link
        def cross_volume(source, destination, **kwargs):
            if Path(source).parent == repository: raise OSError(errno.EXDEV, 'Different volumes')
            return original_link(source, destination, **kwargs)
        with patch('incremental_backups.os.link', side_effect=cross_volume), patch('incremental_backups.storage_guard') as guard:
            self.incremental._view({'repositoryPath': str(repository), 'files': [self.entry]}, self.root / 'view')
        self.assertTrue(guard.called)
        self.assertEqual((self.root / 'view/config').read_bytes(), self.source.read_bytes())
        self.assertNotEqual((self.root / 'view/config').stat().st_ino, (repository / 'config').stat().st_ino)

    def test_unsupported_publication_filesystem_leaves_no_partial_object(self):
        target = self.root / 'target/config'
        with patch('incremental_backups.os.link', side_effect=OSError(errno.EOPNOTSUPP, 'Unsupported')):
            with self.assertRaisesRegex(Problem, 'atomically publish'):
                self.incremental._install(self.source, target, self.entry)
        self.assertFalse(target.exists())
        self.assertFalse(list(target.parent.glob('.incoming-*')))

    def test_corrupt_source_or_cancel_never_publishes_and_existing_object_is_never_replaced(self):
        target = self.root / 'target/config'
        with self.assertRaises(Problem): self.incremental._install(self.source, target, {**self.entry, 'sha256': '0' * 64})
        self.assertFalse(target.exists())
        self.assertTrue(self.incremental._install(self.source, target, self.entry))
        inode = target.stat().st_ino
        self.assertFalse(self.incremental._install(self.source, target, self.entry))
        self.assertEqual(inode, target.stat().st_ino)
        self.incremental.service.closed.set()
        with self.assertRaises(Problem): self.incremental._install(self.source, self.root / 'cancelled', self.entry)
        self.assertFalse((self.root / 'cancelled').exists())
        self.assertFalse(list(self.root.rglob('.incoming-*')))


if __name__ == '__main__': unittest.main()
