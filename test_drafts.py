import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from drafts import Drafts, content_hash
from engine import Engine, MAX_TEXT, Problem


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.engine = Engine(self.base / 'home')
        self.pid = self.engine.create('Notes')['id']
        self.root = self.engine.root(self.pid)
        self.original = (self.root / 'README.md').read_text()
        self.drafts = Drafts(self.engine)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, text='My unsaved thought', revision=None, path='README.md', base=None):
        return self.drafts.save(self.pid, path, text, self.original if base is None else base, revision)

    def test_draft_survives_restart_without_writing_source_or_git(self):
        before = self.engine.git(self.root, 'rev-parse', 'HEAD')
        saved = self.save()
        restored = Drafts(Engine(self.engine.home)).get(self.pid, 'README.md')
        self.assertEqual(restored, saved)
        self.assertFalse(restored['stale'])
        self.assertEqual(restored['baseHash'], content_hash(self.original))
        self.assertEqual((self.root / 'README.md').read_text(), self.original)
        self.assertEqual(self.engine.git(self.root, 'rev-parse', 'HEAD'), before)
        self.assertFalse(self.engine.detail(self.pid)['dirty'])
        self.assertEqual(len(restored['revision']), 64)
        for path in (self.engine.home / '.drafts' / self.pid).glob('*.json'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_stale_base_is_visible_and_draft_retained_after_external_edit(self):
        saved = self.save()
        self.engine.edit(self.pid, 'README.md', 'A separate edit')
        restored = self.drafts.get(self.pid, 'README.md')
        self.assertTrue(restored['stale'])
        self.assertEqual(restored['currentHash'], content_hash('A separate edit'))
        self.assertEqual(restored['content'], saved['content'])
        self.assertEqual(restored['baseContent'], self.original)

    def test_new_file_distinguishes_absence_from_empty_file(self):
        first = self.drafts.save(self.pid, 'new.txt', 'A new thought', None, None)
        self.assertIsNone(first['baseHash'])
        self.assertFalse(first['stale'])
        self.assertFalse((self.root / 'new.txt').exists())
        self.engine.edit(self.pid, 'new.txt', '')
        restored = self.drafts.get(self.pid, 'new.txt')
        self.assertTrue(restored['stale'])
        self.assertEqual(restored['currentHash'], content_hash(''))

    def test_save_and_discard_require_exact_revision(self):
        first = self.save()
        second = self.save('A later draft', first['revision'])
        self.assertNotEqual(second['revision'], first['revision'])
        for old in (None, first['revision']):
            with self.assertRaisesRegex(Problem, 'another window'):
                self.save('Do not overwrite', old)
            with self.assertRaises(Problem):
                self.drafts.discard(self.pid, 'README.md', old)
        self.assertEqual(self.drafts.get(self.pid, 'README.md')['content'], 'A later draft')
        self.assertEqual(self.drafts.discard(self.pid, 'README.md', second['revision']), {'path': 'README.md', 'discarded': True})
        self.assertIsNone(self.drafts.get(self.pid, 'README.md'))
        with self.assertRaises(Problem):
            self.drafts.discard(self.pid, 'README.md', second['revision'])
        self.assertEqual((self.root / 'README.md').read_text(), self.original)

    def test_identical_save_preserves_revision(self):
        first = self.save()
        replay = self.save(revision=first['revision'])
        self.assertEqual(first, replay)

    def test_parallel_instances_cannot_silently_replace_draft(self):
        first = self.save()
        other = Drafts(Engine(self.engine.home))
        barrier = threading.Barrier(2)
        outcomes = []
        def write(instance, content):
            barrier.wait(timeout=3)
            try:
                outcomes.append(instance.save(self.pid, 'README.md', content, self.original, first['revision'])['content'])
            except Problem:
                outcomes.append('conflict')
        threads = [threading.Thread(target=write, args=(instance, text)) for instance, text in ((self.drafts, 'one'), (other, 'two'))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=4)
            self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes.count('conflict'), 1)
        self.assertIn(self.drafts.get(self.pid, 'README.md')['content'], ('one', 'two'))

    def test_record_failure_is_atomic_and_preserves_saved_draft(self):
        first = self.save()
        with patch('drafts.os.replace', side_effect=OSError('Disk full')):
            with self.assertRaisesRegex(OSError, 'Disk full'):
                self.save('Cannot persist', first['revision'])
        self.assertEqual(self.drafts.get(self.pid, 'README.md'), first)
        self.assertFalse(list((self.drafts._store / self.pid).glob('.write-*')))

    def test_unsupported_paths_and_content_are_rejected(self):
        for path in ('../outside', '/tmp/outside', '.git/config', '.env', '.unforge/project.json', 'x\\y'):
            with self.subTest(path=path), self.assertRaises(Problem):
                self.save(path=path)
        for content in ('x' * (MAX_TEXT + 1), '\0', '\ud800', False):
            with self.subTest(content_type=type(content).__name__), self.assertRaises(Problem):
                self.save(text=content)
        with self.assertRaises(Problem):
            self.drafts.save(self.pid, 'README.md', 'Small', 'x' * (MAX_TEXT + 1), None)
        self.assertEqual(self.drafts.list(self.pid)['drafts'], [])

    def test_source_symlink_does_not_expose_target_and_draft_remains_available(self):
        first = self.save()
        (self.root / 'README.md').unlink()
        outside = self.base / 'private'
        outside.write_text('PRIVATE DATA')
        (self.root / 'README.md').symlink_to(outside)
        restored = self.drafts.get(self.pid, 'README.md')
        self.assertTrue(restored['stale'])
        self.assertFalse(restored['currentReadable'])
        self.assertIsNone(restored['currentHash'])
        self.assertEqual(restored['content'], first['content'])
        self.assertNotIn('PRIVATE DATA', json.dumps(restored))

    def test_directory_storage_symlink_is_rejected(self):
        outside = self.base / 'other-storage'
        outside.mkdir()
        another_home = Engine(self.base / 'other-home')
        (another_home.home / '.drafts').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(Problem, 'directory owned'):
            Drafts(another_home)
        self.assertEqual(list(outside.iterdir()), [])

    def test_draft_inventory_omits_text_and_reports_corruption(self):
        first = self.save()
        inventory = self.drafts.list(self.pid)
        self.assertEqual(inventory['drafts'][0]['path'], 'README.md')
        self.assertNotIn('content', inventory['drafts'][0])
        self.assertNotIn('baseContent', inventory['drafts'][0])
        stored = self.drafts._store / self.pid / self.drafts._validate_path('README.md')
        data = json.loads(stored.read_text())
        data['content'] = 'Accidental edit'
        stored.write_text(json.dumps(data))
        with self.assertRaisesRegex(Problem, 'checksum'):
            self.drafts.get(self.pid, 'README.md')
        with self.assertRaisesRegex(Problem, 'checksum'):
            self.save('Do not replace corrupted draft', first['revision'])
        inventory = self.drafts.list(self.pid)
        self.assertEqual(inventory['drafts'], [])
        self.assertEqual(len(inventory['errors']), 1)
        self.assertTrue(stored.exists())

    def test_quota_never_evicts_unwritten_work(self):
        first = self.save()
        with patch('drafts.MAX_DRAFTS', 1):
            with self.assertRaisesRegex(Problem, 'saved drafts'):
                self.drafts.save(self.pid, 'other.txt', 'Other', None, None)
        with patch('drafts.MAX_DRAFT_BYTES', 1):
            with self.assertRaisesRegex(Problem, 'storage limit'):
                self.save('Larger draft', first['revision'])
        self.assertEqual(self.drafts.get(self.pid, 'README.md'), first)


if __name__ == '__main__':
    unittest.main()
