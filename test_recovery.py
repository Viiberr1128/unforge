import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from engine import Engine, Problem
from recovery import Recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.engine = Engine(self.base / 'projects')
        self.project = self.engine.create('Garden', 'A personal garden notebook')
        self.pid = self.project['id']
        self.root = self.engine.root(self.pid)
        self.recovery = Recovery(self.engine)

    def tearDown(self):
        self.temporary.cleanup()

    def ignore(self, *paths):
        self.engine.edit(self.pid, '.gitignore', '\n'.join(paths) + '\n')
        self.engine.save(self.pid, 'Keep local data out of source history')

    def source_capsule(self):
        receipt = self.recovery.create(self.pid, [])
        return receipt, self.recovery._archive(self.pid, receipt['id'])

    def rewrite(self, source, change):
        with tarfile.open(source, 'r:gz') as archive:
            entries = {member.name: archive.extractfile(member).read() for member in archive}
        manifest = json.loads(entries.pop('manifest.json'))
        change(manifest, entries)
        entries['manifest.json'] = json.dumps(manifest).encode()
        return self.make_archive(entries.items())

    def make_archive(self, entries):
        target = self.base / 'incoming.tar.gz'
        with tarfile.open(target, 'w:gz') as archive:
            for name, content in entries:
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        return target

    def test_source_capture_rehearsal_is_durable_and_does_not_run_source(self):
        self.engine.edit(self.pid, 'run.sh', 'touch SHOULD_NOT_EXIST\n')
        self.engine.save(self.pid, 'Record an inert application script')
        receipt, archive = self.source_capsule()
        original_projects = self.engine.projects()
        result = self.recovery.rehearse(self.pid, receipt['id'])
        self.assertTrue(result['ok'])
        self.assertTrue(result['sourceOnly'])
        self.assertFalse(result['applicationExecuted'])
        self.assertEqual(result['sqliteChecks'], 0)
        self.assertNotIn('declared SQLite integrity', result['checks'])
        self.assertEqual(result['capsuleSha256'], receipt['sha256'])
        self.assertEqual(self.engine.projects(), original_projects)
        self.assertFalse((self.root / 'SHOULD_NOT_EXIST').exists())
        state = Recovery(Engine(self.engine.home)).state(self.pid)
        self.assertEqual(state['rehearsals'], [result])
        self.assertEqual(state['capsules'][0]['encryption'], 'none')
        self.assertEqual(receipt['sourceHead'], self.engine.git(self.root, 'rev-parse', 'HEAD').decode().strip())
        name, data = self.recovery.download(self.pid, receipt['id'])
        self.assertTrue(name.endswith('.tar.gz'))
        self.assertEqual(hashlib.sha256(data).hexdigest(), receipt['sha256'])
        self.assertTrue(archive.is_file())

    def test_live_sqlite_backup_uploads_and_empty_directories_restore_independently(self):
        self.ignore('uploads/', 'records.sqlite*', 'settings.json', '.env.local')
        (self.root / 'uploads/empty').mkdir(parents=True)
        (self.root / 'uploads/seed.png').write_bytes(b'\x00\x01uploaded bytes\xff')
        (self.root / 'settings.json').write_text('{"locale":"fr"}')
        (self.root / '.env.local').write_text('DO_NOT_EXPORT=private')
        database = sqlite3.connect(self.root / 'records.sqlite')
        try:
            database.execute('PRAGMA journal_mode=WAL')
            database.execute('CREATE TABLE notes (body TEXT)')
            database.execute('INSERT INTO notes VALUES (?)', ('A living record',))
            database.commit()
            assets = [{'path': 'uploads', 'kind': 'directory'}, {'path': 'records.sqlite', 'kind': 'sqlite'},
                      {'path': 'settings.json', 'kind': 'file'}]
            receipt = self.recovery.create(self.pid, assets)
            database.execute('UPDATE notes SET body = ?', ('Changed after capture',))
            database.commit()
            (self.root / 'uploads/seed.png').write_bytes(b'Later upload')
            result = self.recovery.rehearse(self.pid, receipt['id'])
            self.assertEqual(result['sqliteChecks'], 1)
            self.assertFalse(result['sourceOnly'])
            restored = self.recovery.restore(self.pid, receipt['id'])
            restored_root = self.engine.root(restored['id'])
            self.assertNotEqual(restored['id'], self.pid)
            self.assertEqual(restored['name'], 'Garden (recovered)')
            self.assertEqual(self.engine.detail(self.pid)['name'], 'Garden')
            self.assertIn(receipt['sourceHead'], [version['id'] for version in restored['history']])
            self.assertEqual((restored_root / 'uploads/seed.png').read_bytes(), b'\x00\x01uploaded bytes\xff')
            self.assertTrue((restored_root / 'uploads/empty').is_dir())
            self.assertFalse((restored_root / '.env.local').exists())
            self.assertEqual((restored_root / 'settings.json').read_text(), '{"locale":"fr"}')
            with sqlite3.connect(restored_root / 'records.sqlite') as recovered:
                self.assertEqual(recovered.execute('SELECT body FROM notes').fetchall(), [('A living record',)])
            self.assertEqual(database.execute('SELECT body FROM notes').fetchall(), [('Changed after capture',)])
            self.assertFalse(restored['dirty'])
            self.assertEqual(self.recovery.list(restored['id'])[0]['sha256'], receipt['sha256'])
        finally:
            database.close()

    def test_import_keeps_its_own_capsule_and_original_project(self):
        receipt, archive = self.source_capsule()
        downloaded = self.base / 'backup.tar.gz'
        downloaded.write_bytes(archive.read_bytes())
        detail = self.recovery.import_capsule(str(downloaded))
        downloaded.unlink()
        self.assertEqual(len(self.engine.projects()), 2)
        self.assertEqual(detail['name'], 'Garden (recovered)')
        self.assertNotEqual(detail['name'], self.project['name'])
        self.assertEqual((self.engine.root(detail['id']) / 'README.md').read_text(), (self.root / 'README.md').read_text())
        self.assertTrue(self.recovery.rehearse(detail['id'], receipt['id'])['ok'])

    def test_recovered_project_name_is_bounded_including_suffix(self):
        original = self.engine.create('G' * 120)
        receipt = self.recovery.create(original['id'], [])
        restored = self.recovery.restore(original['id'], receipt['id'])
        self.assertEqual(restored['name'], 'G' * 68 + ' (recovered)')
        self.assertEqual(len(restored['name']), 80)
        self.assertEqual(self.engine.detail(original['id'])['name'], 'G' * 120)

    def test_unsaved_source_and_unignored_data_are_refused(self):
        self.engine.edit(self.pid, 'README.md', 'Uncommitted source')
        with self.assertRaisesRegex(Problem, 'Save source'):
            self.recovery.create(self.pid, [])
        self.engine.save(self.pid, 'Save source')
        (self.root / 'untracked.txt').write_text('Not ignored')
        with self.assertRaisesRegex(Problem, 'Save source'):
            self.recovery.create(self.pid, [{'path': 'untracked.txt', 'kind': 'file'}])
        self.assertEqual(self.recovery.list(self.pid), [])

    def test_asset_declarations_reject_reserved_paths_collisions_and_overlap(self):
        self.ignore('data/', 'DATA/', 'cache.sqlite', '.env.local')
        cases = [None, {}, [{'path': 'x', 'kind': 'unknown'}], [{'path': '../escape', 'kind': 'file'}],
                 [{'path': '.env.local', 'kind': 'file'}], [{'path': '.unforge/private', 'kind': 'file'}],
                 [{'path': 'README.md', 'kind': 'file'}],
                 [{'path': 'data', 'kind': 'directory'}, {'path': 'data/child', 'kind': 'file'}],
                 [{'path': 'data', 'kind': 'directory'}, {'path': 'DATA', 'kind': 'directory'}],
                 [{'path': 'caf\u00e9', 'kind': 'directory'}, {'path': 'cafe\u0301', 'kind': 'directory'}]]
        for assets in cases:
            with self.subTest(assets=assets), self.assertRaises(Problem):
                self.recovery.create(self.pid, assets)
        self.assertEqual(self.recovery.list(self.pid), [])

    def test_directory_symlink_and_reserved_nested_content_are_refused(self):
        self.ignore('data/')
        (self.root / 'data').mkdir()
        link = self.root / 'data/outside'
        link.symlink_to(self.base)
        with self.assertRaisesRegex(Problem, 'Symbolic links'):
            self.recovery.create(self.pid, [{'path': 'data', 'kind': 'directory'}])
        link.unlink()
        (self.root / 'data/.env').write_text('Never silently include')
        with self.assertRaisesRegex(Problem, 'Private or reserved'):
            self.recovery.create(self.pid, [{'path': 'data', 'kind': 'directory'}])

    def test_added_directory_file_during_capture_aborts_without_receipt(self):
        self.ignore('data/')
        (self.root / 'data').mkdir()
        (self.root / 'data/original').write_text('Original')
        copy = self.recovery._copy_file

        def change_after_copy(source, target):
            copy(source, target)
            (self.root / 'data/added-during-copy').write_text('Changed collection')

        with patch.object(self.recovery, '_copy_file', side_effect=change_after_copy):
            with self.assertRaisesRegex(Problem, 'directory changed'):
                self.recovery.create(self.pid, [{'path': 'data', 'kind': 'directory'}])
        self.assertEqual(self.recovery.list(self.pid), [])

    def test_file_changed_during_copy_is_detected(self):
        source, target = self.base / 'source', self.base / 'target'
        source.write_text('Before')
        original_stat = Path.lstat

        def change_before_final_stat(path, *args, **kwargs):
            if path == source:
                source.write_text('Changed while the copy was underway')
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, 'lstat', change_before_final_stat):
            with self.assertRaisesRegex(Problem, 'changed during capture'):
                self.recovery._copy_file(source, target)

    def test_corrupt_archive_invalidates_previous_rehearsal(self):
        receipt, archive = self.source_capsule()
        self.recovery.rehearse(self.pid, receipt['id'])
        archive.write_bytes(archive.read_bytes() + b'corruption')
        with self.assertRaisesRegex(Problem, 'hash does not match'):
            self.recovery.download(self.pid, receipt['id'])
        self.assertEqual(self.recovery.state(self.pid)['rehearsals'], [])

    def test_corrupt_or_symlink_receipt_is_ignored_and_refused(self):
        receipt, _ = self.source_capsule()
        receipt_path = self.recovery._store(self.pid) / (receipt['id'] + '.json')
        receipt_path.write_text('[]')
        self.assertEqual(self.recovery.list(self.pid), [])
        with self.assertRaisesRegex(Problem, 'Invalid recovery capsule receipt'):
            self.recovery.download(self.pid, receipt['id'])
        receipt_path.unlink()
        receipt_path.symlink_to(self.root / 'README.md')
        with self.assertRaises(Problem):
            self.recovery.download(self.pid, receipt['id'])

    def test_unsafe_tar_paths_and_case_or_unicode_aliases_are_rejected(self):
        for name in ('../escape', '/absolute', 'assets/../escape', 'assets\\escape', 'assets/x\nname'):
            archive = self.make_archive([(name, b'bad')])
            with self.subTest(name=name), self.assertRaises(Problem):
                self.recovery.import_capsule(str(archive))
        for first, second in [('assets/X', 'assets/x'), ('assets/caf\u00e9', 'assets/cafe\u0301')]:
            archive = self.make_archive([(first, b'1'), (second, b'2')])
            with self.subTest(first=first), self.assertRaisesRegex(Problem, 'Duplicate capsule path'):
                self.recovery.import_capsule(str(archive))
        self.assertEqual(len(self.engine.projects()), 1)
        self.assertFalse((self.base / 'escape').exists())

    def test_tar_symbolic_and_hard_links_are_rejected(self):
        archive = self.base / 'links.tar.gz'
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            with tarfile.open(archive, 'w:gz') as output:
                member = tarfile.TarInfo('assets/link')
                member.type, member.linkname = kind, str(self.root / 'README.md')
                output.addfile(member)
            with self.subTest(kind=kind), self.assertRaisesRegex(Problem, 'Unsafe capsule archive entry'):
                self.recovery.import_capsule(str(archive))

    def test_tar_directory_prefix_aliases_are_rejected(self):
        cases = [('assets/Data/one', 'assets/data/two'),
                 ('assets/caf\u00e9/one', 'assets/cafe\u0301/two'),
                 ('assets/cache', 'assets/Cache/child')]
        for first, second in cases:
            archive = self.make_archive([(first, b'First'), (second, b'Second')])
            with self.subTest(first=first), self.assertRaisesRegex(Problem, 'paths collide'):
                self.recovery.import_capsule(str(archive))

    def test_hash_mismatch_and_undeclared_contents_are_rejected(self):
        _, original = self.source_capsule()
        wrong_hash = self.rewrite(original, lambda manifest, entries: manifest['files'].update({'project.bundle': '0' * 64}))
        with self.assertRaisesRegex(Problem, 'hash verification'):
            self.recovery.import_capsule(str(wrong_hash))
        undeclared = self.rewrite(original, lambda manifest, entries: entries.update({'assets/extra': b'Undeclared'}))
        with self.assertRaisesRegex(Problem, 'does not cover'):
            self.recovery.import_capsule(str(undeclared))
        self.assertEqual(len(self.engine.projects()), 1)

    def test_recorded_revision_must_be_exact_bundle_head(self):
        older = self.engine.git(self.root, 'rev-parse', 'HEAD').decode().strip()
        self.engine.edit(self.pid, 'README.md', 'Newer version')
        self.engine.save(self.pid, 'Update source')
        _, original = self.source_capsule()
        changed = self.rewrite(original, lambda manifest, entries: manifest.update(sourceHead=older))
        with self.assertRaisesRegex(Problem, 'revision does not match'):
            self.recovery.import_capsule(str(changed))
        self.assertEqual(len(self.engine.projects()), 1)

    def test_asset_with_valid_hash_cannot_enter_source_history(self):
        _, original = self.source_capsule()

        def add_unignored(manifest, entries):
            content = b'Private local data'
            manifest['assets'] = [{'path': 'public-data.txt', 'kind': 'file'}]
            entries['assets/public-data.txt'] = content
            manifest['files']['assets/public-data.txt'] = hashlib.sha256(content).hexdigest()

        changed = self.rewrite(original, add_unignored)
        with self.assertRaisesRegex(Problem, 'data must be ignored'):
            self.recovery.import_capsule(str(changed))
        self.assertEqual(len(self.engine.projects()), 1)

    def test_bad_sqlite_contents_clean_up_the_new_project(self):
        self.ignore('records.sqlite')
        with sqlite3.connect(self.root / 'records.sqlite') as database:
            database.execute('CREATE TABLE notes (body TEXT)')
        receipt = self.recovery.create(self.pid, [{'path': 'records.sqlite', 'kind': 'sqlite'}])

        def corrupt_database(manifest, entries):
            content = b'not a SQLite database'
            entries['assets/records.sqlite'] = content
            manifest['files']['assets/records.sqlite'] = hashlib.sha256(content).hexdigest()

        changed = self.rewrite(self.recovery._archive(self.pid, receipt['id']), corrupt_database)
        with self.assertRaisesRegex(Problem, 'SQLite recovery asset failed'):
            self.recovery.import_capsule(str(changed))
        self.assertEqual(len(self.engine.projects()), 1)

    def test_missing_empty_directory_is_not_reported_as_recovered(self):
        self.ignore('empty/')
        (self.root / 'empty').mkdir()
        receipt = self.recovery.create(self.pid, [{'path': 'empty', 'kind': 'directory'}])
        changed = self.rewrite(self.recovery._archive(self.pid, receipt['id']), lambda manifest, entries: manifest.update(directories=[]))
        with self.assertRaisesRegex(Problem, 'directory is missing'):
            self.recovery.import_capsule(str(changed))
        self.assertEqual(len(self.engine.projects()), 1)

    def test_bounded_gzip_expansion_before_tar_metadata_parsing(self):
        archive = self.base / 'expansion.tar.gz'
        archive.write_bytes(gzip.compress(b'\0' * (4 * 1024 * 1024 + 20000)))
        with patch('recovery.LIMIT', 8192), patch('recovery.FILE_LIMIT', 0):
            with self.assertRaisesRegex(Problem, 'decompression limits'):
                self.recovery.import_capsule(str(archive))

    def test_failed_adoption_removes_only_the_new_project(self):
        receipt, _ = self.source_capsule()
        with patch.object(self.recovery, '_adopt', side_effect=OSError('Disk full')):
            with self.assertRaisesRegex(OSError, 'Disk full'):
                self.recovery.restore(self.pid, receipt['id'])
        self.assertEqual(len(self.engine.projects()), 1)
        self.assertTrue(self.engine.root(self.pid).is_dir())


if __name__ == '__main__':
    unittest.main()
