import json
from pathlib import Path
import tempfile
import unittest

from engine import Engine, Problem
from projects import Projects


class ProjectsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / 'source'
        self.source.mkdir()
        self.engine = Engine(self.base / 'home')
        self.projects = Projects(self.engine)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, name, content='hello'):
        file = self.source / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        return file

    def adopt(self, **kwargs):
        inventory = self.projects.inventory(str(self.source))
        return self.projects.import_folder(str(self.source), expected_revision=inventory['revision'], **kwargs)

    def git(self, *args):
        return self.engine.git(self.source, *args)

    def init(self):
        self.git('init', '--template=', '-b', 'main')

    def test_plain_folder_and_original_are_independent(self):
        self.put('src/main.py', 'print("hello")')
        result = self.adopt()
        pid = result['project']['id']
        self.assertEqual(self.projects.read_file(pid, 'src/main.py')['content'], 'print("hello")')
        self.assertFalse((self.source / '.git').exists())
        self.assertFalse(result['adoption']['partial'])
        self.assertEqual(self.projects.adoption(pid)['sourcePath'], str(self.source))
        self.assertFalse((self.engine.root(pid) / '.unforge/adoption.json').exists())

    def test_partial_requires_review_and_reports_templates_and_data(self):
        self.put('main.py')
        self.put('.env', 'PRIVATE=not-a-real-secret')
        self.put('.env.example', 'PORT=3000')
        self.put('data/store.sqlite', 'fixture data')
        self.put('node_modules/package/index.js')
        inventory = self.projects.inventory(str(self.source))
        self.assertTrue(inventory['partial'])
        self.assertEqual({x['path'] for x in inventory['skipped']}, {'.env', '.env.example', 'data/', 'node_modules/'})
        with self.assertRaisesRegex(Problem, 'partial copy'):
            self.projects.import_folder(str(self.source), expected_revision=inventory['revision'])
        result = self.projects.import_folder(str(self.source), expected_revision=inventory['revision'], allow_partial=True)
        self.assertFalse((self.engine.root(result['project']['id']) / '.env').exists())
        self.assertTrue((self.source / '.env').exists())

    def test_stale_inventory_is_rejected_and_staging_cleaned(self):
        self.put('README.md', 'before')
        inventory = self.projects.inventory(str(self.source))
        self.put('README.md', 'after')
        with self.assertRaisesRegex(Problem, 'inventory changed'):
            self.projects.import_folder(str(self.source), expected_revision=inventory['revision'])
        self.assertEqual(list(self.engine.home.iterdir()), [])

    def test_dirty_git_preserves_refs_history_and_current_work(self):
        self.init()
        self.put('main.txt', 'committed')
        self.git('add', '.')
        self.git('commit', '-m', 'Original')
        original = self.git('rev-parse', 'HEAD').decode().strip()
        self.git('branch', 'other')
        self.git('tag', 'v1')
        self.put('main.txt', 'dirty')
        self.put('new.txt', 'untracked')
        old_status = self.git('status', '--porcelain')
        old_index = (self.source / '.git/index').read_bytes()
        result = self.adopt()
        root = self.engine.root(result['project']['id'])
        self.assertEqual((root / 'main.txt').read_text(), 'dirty')
        self.assertEqual((root / 'new.txt').read_text(), 'untracked')
        self.assertEqual(self.engine.git(root, 'rev-parse', 'refs/heads/main').decode().strip(), original)
        self.assertEqual(self.engine.git(root, 'rev-parse', 'refs/heads/other').decode().strip(), original)
        self.assertEqual(self.engine.git(root, 'rev-parse', 'refs/tags/v1').decode().strip(), original)
        self.assertEqual(self.git('status', '--porcelain'), old_status)
        self.assertEqual((self.source / '.git/index').read_bytes(), old_index)

    def test_unborn_git_can_be_imported_without_original_commit(self):
        self.init()
        self.put('app.py')
        result = self.adopt()
        self.assertTrue(result['project']['history'])
        self.assertFalse((self.source / '.git/refs/heads/main').exists())

    def test_worktree_import_preserves_common_refs(self):
        self.init()
        self.put('main.txt', 'first')
        self.git('add', '.')
        self.git('commit', '-m', 'Original')
        linked = self.base / 'linked'
        self.git('worktree', 'add', '-b', 'linked', str(linked))
        (linked / 'main.txt').write_text('linked dirty')
        inventory = self.projects.inventory(str(linked))
        self.assertEqual(inventory['git']['kind'], 'worktree')
        result = self.projects.import_folder(str(linked), expected_revision=inventory['revision'])
        self.assertEqual((self.engine.root(result['project']['id']) / 'main.txt').read_text(), 'linked dirty')
        self.assertEqual((linked / 'main.txt').read_text(), 'linked dirty')

    def test_source_hooks_and_filters_are_not_executed(self):
        self.init()
        self.put('main.txt')
        self.git('add', '.')
        self.git('commit', '-m', 'Initial')
        trigger = self.base / 'triggered'
        hook = self.source / '.git/hooks/post-checkout'
        hook.parent.mkdir(exist_ok=True)
        hook.write_text('#!/bin/sh\ntouch ' + str(trigger) + '\n')
        hook.chmod(0o755)
        with (self.source / '.git/config').open('a') as writer:
            writer.write('\n[filter "evil"]\n\tclean = touch ' + str(trigger) + '\n\tsmudge = touch ' + str(trigger) + '\n')
        self.put('.gitattributes', '*.txt filter=evil')
        result = self.adopt(allow_partial=True)
        self.assertFalse(trigger.exists())
        self.assertNotIn('evil', (self.engine.root(result['project']['id']) / '.git/config').read_text())

    def test_symlinks_are_explicitly_omitted(self):
        self.put('main.txt')
        secret = self.base / 'private.txt'
        secret.write_text('private')
        (self.source / 'linked.txt').symlink_to(secret)
        inventory = self.projects.inventory(str(self.source))
        self.assertTrue(inventory['partial'])
        self.assertEqual(inventory['skipped'][0]['path'], 'linked.txt')
        result = self.adopt(allow_partial=True)
        self.assertFalse((self.engine.root(result['project']['id']) / 'linked.txt').exists())

    def test_pagination_and_demand_read_cover_more_than_100_files(self):
        for index in range(215):
            self.put(f'src/file-{index:03}.txt', str(index))
        result = self.adopt()
        pid = result['project']['id']
        seen, cursor = [], 0
        while cursor is not None:
            page = self.projects.files(pid, cursor, 40)
            seen.extend(x['path'] for x in page['files'])
            cursor = page['nextCursor']
        self.assertEqual(len(seen), 216)
        self.assertEqual(len(set(seen)), 216)
        self.assertEqual(self.projects.read_file(pid, 'src/file-214.txt')['content'], '214')
        with self.assertRaises(Problem):
            self.projects.read_file(pid, '../private.txt')

    def test_binary_and_large_files_are_listed_but_not_read(self):
        (self.source / 'image.bin').write_bytes(b'\0binary')
        self.put('large.txt', 'x' * (128 * 1024 + 1))
        result = self.adopt()
        pid = result['project']['id']
        entries = {entry['path']: entry for entry in self.projects.files(pid)['files']}
        self.assertIn('image.bin', entries)
        self.assertFalse(entries['large.txt']['readable'])
        with self.assertRaisesRegex(Problem, 'binary'):
            self.projects.read_file(pid, 'image.bin')
        with self.assertRaises(Problem):
            self.projects.read_file(pid, 'large.txt')

    def test_project_ignore_rules_preserve_tracked_files(self):
        self.init()
        self.put('keep.txt', 'tracked')
        self.git('add', 'keep.txt')
        self.git('commit', '-m', 'Track file')
        self.put('.gitignore', '*.txt\n')
        self.put('private-local.txt', 'private fixture')
        inventory = self.projects.inventory(str(self.source))
        self.assertIn('keep.txt', {entry['path'] for entry in inventory['files']})
        self.assertNotIn('private-local.txt', {entry['path'] for entry in inventory['files']})
        self.assertIn('private-local.txt', {entry['path'] for entry in inventory['skipped']})
        result = self.adopt(allow_partial=True)
        self.assertFalse((self.engine.root(result['project']['id']) / 'private-local.txt').exists())

    def test_git_info_exclude_is_applied_without_default_git_template(self):
        self.init()
        self.put('app.py')
        self.git('add', '.')
        self.git('commit', '-m', 'Initial')
        exclude = self.source / '.git/info/exclude'
        exclude.parent.mkdir()
        exclude.write_text('local-notes.txt\n')
        self.put('local-notes.txt', 'private local fixture')
        old_index = (self.source / '.git/index').read_bytes()
        result = self.adopt(allow_partial=True)
        root = self.engine.root(result['project']['id'])
        self.assertTrue((root / 'app.py').exists())
        self.assertFalse((root / 'local-notes.txt').exists())
        self.assertEqual(exclude.read_text(), 'local-notes.txt\n')
        self.assertEqual((self.source / '.git/index').read_bytes(), old_index)

    def test_tracked_source_under_ambiguous_directories_is_preserved(self):
        self.init()
        paths = ['src/data/catalog.ts', 'uploads/parser/handler.py', 'scripts/backups/export.py']
        for name in paths:
            self.put(name, 'tracked source')
        self.git('add', '.')
        self.git('commit', '-m', 'Track application source')
        self.put('src/data/catalog.ts', 'edited source')
        # Tracked source wins over ignore rules, but neighboring runtime files
        # and whole untracked subdirectories must still be omitted.
        self.put('.gitignore', 'data/\nuploads/\nbackups/\n')
        self.put('src/data/private.json', 'runtime fixture')
        self.put('uploads/parser/raw.txt', 'runtime fixture')
        self.put('scripts/backups/archive/original.txt', 'runtime fixture')
        original_status = self.git('status', '--porcelain')
        result = self.adopt(allow_partial=True)
        root = self.engine.root(result['project']['id'])
        for name in paths:
            self.assertEqual((root / name).read_bytes(), (self.source / name).read_bytes())
        self.assertFalse((root / 'src/data/private.json').exists())
        self.assertFalse((root / 'uploads/parser/raw.txt').exists())
        self.assertFalse((root / 'scripts/backups/archive').exists())
        self.assertEqual(self.git('status', '--porcelain'), original_status)

    def test_tracked_status_does_not_bypass_private_or_runtime_boundaries(self):
        self.init()
        self.put('data/source.py')
        blocked = ['data/.env', 'data/credentials.json', 'data/key.pem', 'data/live.sqlite',
                   'data/node_modules/pkg/source.js', 'backups/secrets/private.txt',
                   'uploads/.recovery/old.py']
        for name in blocked:
            self.put(name, 'synthetic private fixture')
        external = self.base / 'external.txt'
        external.write_text('external fixture')
        (self.source / 'data/linked.py').symlink_to(external)
        self.git('add', '.')
        self.git('commit', '-m', 'Boundary fixtures')
        result = self.adopt(allow_partial=True)
        root = self.engine.root(result['project']['id'])
        self.assertTrue((root / 'data/source.py').exists())
        for name in blocked + ['data/linked.py']:
            self.assertFalse((root / name).exists(), name)
        self.assertEqual(external.read_text(), 'external fixture')

    def test_invalid_page_arguments(self):
        self.put('app.py')
        pid = self.adopt()['project']['id']
        for cursor, limit in [(-1, 10), (0, 0), (True, 10), (0, 501)]:
            with self.assertRaises(Problem):
                self.projects.files(pid, cursor, limit)

    def test_shallow_history_is_explicit_partial_snapshot(self):
        self.init()
        self.put('app.py')
        (self.source / '.git/shallow').write_text('fixture')
        inventory = self.projects.inventory(str(self.source))
        self.assertEqual(inventory['git']['history'], 'unsupported')
        result = self.adopt(allow_partial=True)
        self.assertTrue(result['adoption']['partial'])


if __name__ == '__main__':
    unittest.main()
