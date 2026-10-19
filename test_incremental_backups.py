"""Temporary local vaults only; no cloud provider or user workspace operations."""
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import sys
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

    def test_both_backup_formats_restore_lane_links_without_writing_original_checkout(self):
        from lanes import Lanes
        self.engine.save(self.project['id'],'Prepare lane recovery fixture')
        lanes=Lanes(self.engine);lane=lanes.create(self.project['id'],'Recover lane')
        lanes.write(self.project['id'],lane['id'],'saved.txt','saved lane revision')
        lanes.save(self.project['id'],lane['id'],'Save recovered lane fixture')
        lanes.write(self.project['id'],lane['id'],'draft.txt','unsaved original bytes')
        original=Path(lane['path'])
        original_head=self.engine.git(original,'rev-parse','HEAD')
        for standalone in (True,False):
            with self.subTest(standalone=standalone):
                job=self.default_backup(standalone=standalone)
                recovered=self.root/('lane-recovered-v1' if standalone else 'lane-recovered-v2')
                result=self.service.restore(str(self.point(job)),self.password,str(recovered))
                self.assertIsInstance(result['recoveryLinks'],dict)
                self.assertEqual(result['recoveryLinks']['managedLanesRelocated'],1)
                restored_manifest=self.service.verify_workspace(recovered)
                self.assertEqual(result['files'],restored_manifest['fileCount'])
                self.assertGreater(result['files'],result['capturedFiles'])
                replacement=Engine(recovered);recovered_lanes=Lanes(replacement)
                loaded=recovered_lanes.get(self.project['id'],lane['id'])
                self.assertTrue(Path(loaded['path']).is_relative_to(recovered))
                self.assertEqual((Path(loaded['path'])/'draft.txt').read_text(),'unsaved original bytes')
                recovered_lanes.write(self.project['id'],lane['id'],'draft.txt','changed recovered lane only')
                recovered_lanes.save(self.project['id'],lane['id'],'Save only recovered lane')
                self.assertEqual((original/'draft.txt').read_text(),'unsaved original bytes')
                self.assertEqual(self.engine.git(original,'rev-parse','HEAD'),original_head)

    def test_exact_stream_readback_releases_capture_before_verification_and_restores_independently(self):
        original = self.service._stream_command
        observed = []
        def verify(repository, key, consume, *args):
            if args[-1] == '/':
                observed.append(True)
                self.assertFalse(any((folder / 'workspace').exists() for folder in self.incremental.root.glob('snapshot-*')))
                self.assertFalse(any((folder / 'verification').exists() for folder in self.incremental.root.glob('snapshot-*')))
            return original(repository, key, consume, *args)
        with patch.object(self.service, '_stream_command', side_effect=verify): first = self.backup()
        self.assertEqual(observed, [True])
        self.assertTrue(first['verifiedLocally']); self.assertFalse(first['restoredLocally'])
        self.assertEqual(first['verification']['method'], 'exact-snapshot-stream')
        self.assertIn('verify', first['phaseDurationsSeconds'])
        second = self.backup()
        self.assertLess(second['changedSourceBytes'], first['changedSourceBytes'])
        verification = second['destinations'][0]['publicationVerification']
        self.assertGreater(verification['reusedObjectsMetadataChecked'],0)
        self.assertFalse(verification['reusedContentReread'])
        self.assertGreater(verification['newObjectsContentVerified'],0)
        self.assertEqual((self.recover(self.point(first)) / 'personal.txt').read_text(), 'first revision')

    def test_legacy_publication_migration_does_not_read_unchanged_dataless_packs(self):
        first = self.default_backup(); vault = self.point(first).parent.parent
        for cache in self.incremental.root.glob('publication-*.json'): cache.unlink()
        reused = {str(vault/'repository'/entry['path']) for entry in self.incremental.inspect(self.point(first),verify=False)['files']}
        original_digest = digest
        def guarded_digest(path,*args):
            if str(path) in reused: self.fail('Routine migration opened an unchanged reused object')
            return original_digest(path,*args)
        with patch('incremental_backups.digest',side_effect=guarded_digest): second = self.default_backup()
        verification = second['destinations'][0]['publicationVerification']
        self.assertGreater(verification['legacyObjectsMetadataMigrated'],0)
        self.assertGreater(verification['legacyMetadataOnlyObjects'],0)
        self.assertEqual(verification['legacyBaselinePointId'],first['id'])
        self.assertEqual((self.recover(self.point(second))/'personal.txt').read_text(),'first revision')

    def test_upload_metadata_refresh_does_not_open_packs_and_rejects_missing_required_object(self):
        first=self.default_backup();point=self.point(first);vault=point.parent.parent
        self.service.configure([{'kind':'icloud','path':str(self.destination)}])
        self.service.jobs[first['id']]['destinations'][0]['kind']='icloud'
        count=sum(1 for path in vault.rglob('*') if path.is_file())
        original_digest=digest
        def metadata_digest(path,*args):
            if Path(path).is_relative_to(vault/'repository'):self.fail('Upload check requested ciphertext content')
            return original_digest(path,*args)
        with patch('backups.cloud_helper',return_value=Path('/fixture-helper')),patch.object(self.service,'_cloud_report',return_value={'state':'uploaded','files':count,'uploaded':count}),patch('incremental_backups.digest',side_effect=metadata_digest):
            result=self.service.refresh_cloud(first['id'])
            self.assertEqual(result['destinations'][0]['state'],'cloud_uploaded')
            evidence=result['destinations'][0]['uploadEvidence']
            self.assertTrue(evidence['metadataOnly']);self.assertFalse(evidence['contentVerified'])
            pack=next(path for path in (vault/'repository/data').rglob('*') if path.is_file());pack.unlink()
            result=self.service.refresh_cloud(first['id'])
            self.assertEqual(result['destinations'][0]['state'],'upload_pending')
            self.assertIn('missing or has changed size',result['destinations'][0]['uploadEvidence'])

    def test_changed_private_object_cannot_hide_behind_cached_hash(self):
        first = self.backup()
        private = next(path for path in (self.incremental.repository / 'data').rglob('*') if path.is_file())
        saved = private.read_bytes(); private.chmod(0o600); private.write_bytes(bytes([saved[0] ^ 1]) + saved[1:])
        self.backup('failed')
        self.assertEqual(private.read_bytes(), bytes([saved[0] ^ 1]) + saved[1:])

    @unittest.skipUnless(sys.platform == 'darwin', 'APFS clone capture is macOS-specific')
    def test_phase_capacity_preserves_reserve_and_succeeds_without_four_plaintext_copies(self):
        (self.source / 'media.bin').write_bytes(os.urandom(8 * 1024**2))
        capacity = SimpleNamespace(total=500 * 1024**3, used=490 * 1024**3, free=10 * 1024**3 + 32 * 1024**2)
        with patch('backups.shutil.disk_usage', return_value=capacity): result = self.backup()
        self.assertGreater(result['capture']['clonedFiles'], 0)
        self.assertTrue(all(plan['reserveBytes'] == 10 * 1024**3 for plan in result['storagePlan']))
        self.assertTrue(all(plan['requiredBytes'] < 32 * 1024**2 for plan in result['storagePlan']))
        self.assertEqual((self.recover(self.point(result)) / 'media.bin').stat().st_size, 8 * 1024**2)

    def test_retention_review_lists_only_whole_sealed_generation_without_deleting(self):
        self.incremental = IncrementalBackups(self.service, max_points=1)
        first = self.backup(); second = self.backup()
        review = self.service.state()['retentionReview']
        self.assertFalse(review['automaticPruning'])
        candidate = next(item for item in review['generations'] if item['reviewCandidate'])
        self.assertTrue(candidate['sealed']); self.assertFalse(candidate['active'])
        self.assertFalse(candidate['deletionAvailable'])
        self.assertTrue(self.point(first).exists()); self.assertTrue(self.point(second).exists())

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
        self.incremental = IncrementalBackups(SimpleNamespace(root=self.root, closed=threading.Event(),
                                                             lock=threading.RLock(), jobs={}))
        self.source = self.root / 'ciphertext'; self.source.write_bytes(b'encrypted fixture bytes')
        self.entry = {'path': 'config', 'bytes': self.source.stat().st_size, 'sha256': digest(self.source)}

    def tearDown(self): self.tmp.cleanup()

    def publication_context(self, *, legacy=False):
        vault=self.root/'vault.ufvault';repository=vault/'repository';repository.mkdir(parents=True)
        target=repository/'config';target.write_bytes(self.source.read_bytes())
        context=self.incremental._publication_evidence(vault,{'repositoryId':'a'*64,'writerId':'b'*32})
        baseline=dict(self.entry)
        if not legacy:baseline.update(fingerprint=self.incremental._fingerprint(target.stat()),evidence='content-readback')
        context['legacy' if legacy else 'prior']['config']=baseline
        self.incremental._publication=context
        return target,context

    def test_unchanged_dataless_reused_object_is_never_opened_or_digested(self):
        target,context=self.publication_context()
        with patch.object(self.incremental,'_dataless',return_value=True), patch('incremental_backups.digest',side_effect=AssertionError('Hydration requested')):
            self.assertFalse(self.incremental._install(self.source,target,self.entry))
        self.assertEqual(context['verification']['reusedObjectsMetadataChecked'],1)
        self.assertFalse(context['verification']['reusedContentReread'])

    def test_dataless_changed_identity_and_previously_published_missing_object_fail_without_read(self):
        target,context=self.publication_context()
        context['prior']['config']['fingerprint'][4]-=1
        with patch.object(self.incremental,'_dataless',return_value=True),patch('incremental_backups.digest',side_effect=AssertionError('Hydration requested')):
            with self.assertRaisesRegex(Problem,'evicted destination object has no matching trusted identity'):
                self.incremental._install(self.source,target,self.entry)
        target.unlink()
        with self.assertRaisesRegex(Problem,'previously published recovery object is missing'):
            self.incremental._install(self.source,target,self.entry)
        self.assertFalse(target.exists())

    def test_ordinary_same_size_tamper_invalidates_fingerprint_and_hash_fails(self):
        target,context=self.publication_context()
        saved=target.stat();target.write_bytes(b'x'*self.entry['bytes'])
        os.utime(target,ns=(saved.st_atime_ns,saved.st_mtime_ns))
        self.assertNotEqual(target.stat().st_ctime_ns,context['prior']['config']['fingerprint'][4])
        with self.assertRaisesRegex(Problem,'incomplete or damaged'):
            self.incremental._install(self.source,target,self.entry)

    def test_ordinary_metadata_change_reverifies_content_and_legacy_migration_is_explicit(self):
        target,context=self.publication_context()
        saved=target.stat();os.utime(target,ns=(saved.st_atime_ns,saved.st_mtime_ns+10000))
        self.assertFalse(self.incremental._install(self.source,target,self.entry))
        self.assertEqual(context['verification']['existingObjectsContentReverified'],1)
        self.assertTrue(context['verification']['reusedContentReread'])
        self.assertFalse(context['verification']['unchangedReusedContentReread'])
        self.assertIn('digestVerifiedAt',context['verification'])
        self.assertEqual(context['observed']['config']['evidence'],'content-readback')

    def test_final_publication_metadata_transition_rechecks_bytes_before_commit(self):
        target,context=self.publication_context();context['prior'].clear()
        self.incremental._publication_check(target,self.entry,created=True)
        saved=target.stat();os.utime(target,ns=(saved.st_atime_ns,saved.st_mtime_ns+10000))
        with patch('incremental_backups.digest',wraps=digest) as readback:
            self.incremental._finish_publication_inventory(target.parent)
        self.assertEqual(readback.call_count,1)
        self.assertEqual(context['verification']['newObjectsContentVerified'],1)
        self.assertEqual(context['verification']['existingObjectsContentReverified'],1)
        self.assertEqual(context['observed']['config']['fingerprint'],self.incremental._fingerprint(target.stat()))

    def test_final_publication_changed_legacy_object_cannot_remigrate_or_hide_tamper(self):
        target,context=self.publication_context(legacy=True)
        self.incremental._publication_check(target,self.entry)
        saved=target.stat();target.write_bytes(b'x'*self.entry['bytes'])
        os.utime(target,ns=(saved.st_atime_ns,saved.st_mtime_ns))
        with self.assertRaisesRegex(Problem,'incomplete or damaged'):
            self.incremental._finish_publication_inventory(target.parent)
        self.assertEqual(context['verification']['legacyObjectsMetadataMigrated'],1)

    def test_final_publication_evicted_changed_object_is_never_opened(self):
        target,context=self.publication_context()
        self.incremental._publication_check(target,self.entry)
        context['observed']['config']['fingerprint'][4]-=1
        with patch.object(self.incremental,'_dataless',return_value=True),patch('incremental_backups.digest',side_effect=AssertionError('Hydration requested')):
            with self.assertRaisesRegex(Problem,'no matching trusted identity'):
                self.incremental._finish_publication_inventory(target.parent)

    def test_publication_hash_retries_metadata_race_and_bounds_repeated_changes(self):
        target,context=self.publication_context();context['prior'].clear()
        calls=0
        def change_once(path,closed=None):
            nonlocal calls
            calls+=1
            if calls==1:
                info=target.stat();os.utime(target,ns=(info.st_atime_ns,info.st_mtime_ns+10000))
            return digest(path,closed)
        with patch('incremental_backups.digest',side_effect=change_once):
            self.incremental._publication_check(target,self.entry,created=True)
        self.assertEqual(calls,2)
        def keep_changing(path,closed=None):
            info=target.stat();os.utime(target,ns=(info.st_atime_ns,info.st_mtime_ns+10000))
            return digest(path,closed)
        with patch('incremental_backups.digest',side_effect=keep_changing) as readback:
            with self.assertRaisesRegex(Problem,'changed during verification'):
                self.incremental._validate_entry(target.parent,self.entry,allow_hydration=False,metadata_retries=2)
        self.assertEqual(readback.call_count,3)

    @unittest.skipUnless(sys.platform=='darwin','Native APFS clone publication')
    def test_native_clone_publication_has_independent_identity_and_destination_readback(self):
        target,context=self.publication_context();target.unlink();context['prior'].clear()
        context['clonePublication']=True;self.source.chmod(0o400)
        with patch('incremental_backups.digest',wraps=digest) as readback:
            self.assertTrue(self.incremental._install(self.source,target,self.entry))
        self.assertEqual(readback.call_count,2)
        self.assertNotEqual(target.stat().st_ino,self.source.stat().st_ino)
        self.assertEqual(context['allocation']['objectsCloned'],1)
        self.assertEqual(context['allocation']['bytesCopied'],0)
        self.assertEqual(target.stat().st_mode&0o777,0o600)
        self.assertEqual(self.source.stat().st_mode&0o777,0o400)
        expected=target.read_bytes();self.source.chmod(0o600);self.source.write_bytes(b'x'*self.entry['bytes'])
        self.assertEqual(target.read_bytes(),expected)

    @unittest.skipUnless(sys.platform=='darwin','Native APFS clone publication')
    def test_corrupt_cloned_source_fails_before_immutable_destination_install(self):
        target,context=self.publication_context();target.unlink();context['prior'].clear()
        context['clonePublication']=True
        self.source.write_bytes(b'x'*self.entry['bytes'])
        with self.assertRaisesRegex(Problem,'incomplete or damaged'):
            self.incremental._install(self.source,target,self.entry)
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob('.incoming-*')),[])
        self.assertEqual(self.source.read_bytes(),b'x'*self.entry['bytes'])

    def test_publication_clone_fallback_checks_full_allocation_before_copy(self):
        target,context=self.publication_context();target.unlink();context['prior'].clear()
        context['clonePublication']=True;requirements=[]
        def guard(values):requirements.extend(values);return []
        with patch('incremental_backups.clone_file',return_value=False),patch('incremental_backups.storage_guard',side_effect=guard):
            self.assertTrue(self.incremental._install(self.source,target,self.entry))
        self.assertEqual(requirements[0][1],4096)
        self.assertEqual(requirements[1][1],self.entry['bytes']+4096)
        self.assertEqual(context['allocation']['objectsCopied'],1)
        self.assertEqual(context['allocation']['bytesCloned'],0)

    def test_publication_clone_fallback_does_not_allocate_when_full_copy_reserve_fails(self):
        target,context=self.publication_context();target.unlink();context['prior'].clear()
        context['clonePublication']=True
        def guard(values):
            if values[0][1]>4096:raise Problem('Not enough fallback capacity')
            return []
        with patch('incremental_backups.clone_file',return_value=False),patch('incremental_backups.storage_guard',side_effect=guard):
            with self.assertRaisesRegex(Problem,'fallback capacity'):
                self.incremental._install(self.source,target,self.entry)
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob('.incoming-*')),[])

    def test_final_pass_failure_preserves_object_evidence_without_exposing_a_point(self):
        destination=self.root/'destination';destination.mkdir()
        self.incremental.repository.mkdir();(self.incremental.repository/'config').write_bytes(self.source.read_bytes())
        descriptor={'repositoryId':'a'*64,'writerId':'b'*32,'pointId':'c'*32,'files':[self.entry]}
        vault=destination/('unforge-'+descriptor['repositoryId']+'.ufvault')
        old=vault/'points'/('d'*32+'.ufpoint');old.mkdir(parents=True)
        old_marker=old/'POINT.json';old_marker.write_bytes(b'earlier point unchanged')
        with patch.object(self.incremental,'_finish_publication_inventory',side_effect=Problem('Final metadata transition')):
            with self.assertRaisesRegex(Problem,'Final metadata transition'):
                self.incremental._publish({'kind':'folder','path':str(destination)},descriptor)
        self.assertEqual(old_marker.read_bytes(),b'earlier point unchanged')
        self.assertFalse((vault/'points'/('c'*32+'.ufpoint')).exists())
        context=self.incremental._publication_evidence(vault,descriptor)
        cache=json.loads(context['cache'].read_text())
        self.assertEqual(cache['publicationState'],'objects-verified')
        self.assertEqual(cache['objects']['config']['evidence'],'content-readback')
        self.assertTrue(cache['objects']['config']['digestVerifiedAt'])
        self.assertTrue(cache['objects']['config']['identityCheckedAt'])
        self.incremental._publication=context
        target=vault/'repository/config'
        with patch.object(self.incremental,'_dataless',return_value=True),patch('incremental_backups.digest',side_effect=AssertionError('Hydration requested')):
            self.assertFalse(self.incremental._install(self.source,target,self.entry))
            context['prior']['config']['fingerprint'][4]-=1
            with self.assertRaisesRegex(Problem,'no matching trusted identity'):
                self.incremental._install(self.source,target,self.entry)

    def test_full_inspection_bypasses_routine_publication_cache(self):
        target,context=self.publication_context()
        original=digest
        with patch('incremental_backups.digest',side_effect=original) as readback:
            self.incremental._validate_entry(target.parent,self.entry)
        self.assertEqual(readback.call_count,1)

    def test_interrupted_first_publication_materialized_object_is_rehashed_without_baseline(self):
        target,context=self.publication_context();context['prior'].clear()
        with patch('incremental_backups.digest',wraps=digest) as readback:
            self.assertFalse(self.incremental._install(self.source,target,self.entry))
        self.assertEqual(readback.call_count,1)
        self.assertEqual(context['verification']['existingObjectsContentReverified'],1)
        self.assertEqual(context['verification']['legacyObjectsMetadataMigrated'],0)
        self.assertEqual(context['observed']['config']['evidence'],'content-readback')

    def test_unknown_existing_dataless_object_requires_explicit_verification(self):
        target,context=self.publication_context();context['prior'].clear()
        with patch.object(self.incremental,'_dataless',return_value=True),patch('incremental_backups.digest',side_effect=AssertionError('Hydration requested')):
            with self.assertRaisesRegex(Problem,'no matching trusted identity'):
                self.incremental._install(self.source,target,self.entry)

    def test_dataless_metadata_checks_capacity_before_opening(self):
        from incremental_backups import _json
        original_lstat=Path.lstat
        def dataless(path):
            info=original_lstat(path)
            return SimpleNamespace(st_mode=info.st_mode,st_size=info.st_size,st_flags=0x40000000)
        with patch.object(Path,'lstat',dataless),patch('incremental_backups.storage_guard',side_effect=Problem('Insufficient metadata capacity')),patch('incremental_backups.regular_reader',side_effect=AssertionError('Metadata opened before guard')):
            with self.assertRaisesRegex(Problem,'Insufficient metadata capacity'):_json(self.source)

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
