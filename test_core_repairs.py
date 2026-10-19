"""Focused Git interoperability and review-identity regressions in disposable homes."""
import gzip
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

from engine import Engine, Problem
from exchange import Exchange
from integrate import Integrate
from lanes import Lanes


class CoreRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='unforge-core-repair-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.engine = Engine(self.folder / 'home')
        self.pid = self.engine.create('Fixture')['id']
        self.root = self.engine.root(self.pid)
        self.lanes = Lanes(self.engine)
        self.integrate = Integrate(self.engine, self.lanes)

    def commit_external(self, lane, path='external.txt', content='External\n'):
        work = Path(lane['path'])
        (work / path).write_text(content)
        self.engine.git(work, 'add', '--', path)
        self.engine.git(work, 'commit', '-m', 'External Git tool')
        return self.lanes._head(work)

    def test_merge_gates_actual_external_head_and_preserves_main_on_failure(self):
        lane = self.lanes.create(self.pid, 'External')
        self.lanes.write(self.pid, lane['id'], 'saved.txt', 'Saved\n')
        old = self.lanes.save(self.pid, lane['id'], 'Saved change')['head']
        actual = self.commit_external(lane)
        seen = []
        with self.assertRaisesRegex(Problem, 'Checks must pass'):
            self.integrate.merge(self.pid, lane['id'], require_checks=lambda tree: seen.append(tree) or tree == old)
        self.assertEqual(seen, [actual])
        self.assertFalse((self.root / 'external.txt').exists())

    def test_head_movement_during_check_prevents_merge(self):
        lane = self.lanes.create(self.pid, 'Concurrent')
        self.lanes.write(self.pid, lane['id'], 'saved.txt', 'Saved\n')
        self.lanes.save(self.pid, lane['id'], 'Saved change')
        def moved(_tree):
            self.commit_external(lane)
            return True
        with self.assertRaisesRegex(Problem, 'Source changed while checks'):
            self.integrate.merge(self.pid, lane['id'], require_checks=moved)
        self.assertFalse((self.root / 'saved.txt').exists())
        self.assertFalse((self.root / 'external.txt').exists())

    def test_checks_do_not_hold_the_global_engine_lock(self):
        lane = self.lanes.create(self.pid, 'Nonblocking checks')
        self.lanes.write(self.pid, lane['id'], 'saved.txt', 'Saved\n')
        self.lanes.save(self.pid, lane['id'], 'Saved change')
        entered, release, acquired = threading.Event(), threading.Event(), threading.Event()
        outcome = []

        def blocking_check(_tree):
            entered.set()
            release.wait(5)
            return True

        def merge():
            try:
                outcome.append(self.integrate.merge(self.pid, lane['id'], require_checks=blocking_check))
            except Exception as error:  # surfaced below with the original traceback context in the assertion
                outcome.append(error)

        worker = threading.Thread(target=merge)
        worker.start()
        self.assertTrue(entered.wait(2), 'merge did not reach its check callback')

        def observe_lock():
            with self.engine.lock:
                acquired.set()

        observer = threading.Thread(target=observe_lock)
        observer.start()
        self.assertTrue(acquired.wait(2), 'long checks kept the global engine lock')
        release.set()
        worker.join(5)
        observer.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(observer.is_alive())
        if outcome and isinstance(outcome[0], Exception):
            raise outcome[0]
        self.assertTrue((self.root / 'saved.txt').is_file())

    def test_merge_rejects_lane_or_live_head_changed_since_review(self):
        lane = self.lanes.create(self.pid, 'Reviewed')
        self.lanes.write(self.pid, lane['id'], 'saved.txt', 'Saved\n')
        lane = self.lanes.save(self.pid, lane['id'], 'Saved change')
        live = self.lanes._head(self.root)
        with self.assertRaisesRegex(Problem, 'lane changed after review'):
            self.integrate.merge(self.pid, lane['id'], expected_head='0' * 40, expected_live=live)
        self.engine.edit(self.pid, 'live.txt', 'Live moved\n')
        self.engine.save(self.pid, 'Move live source')
        with self.assertRaisesRegex(Problem, 'Working source changed after review'):
            self.integrate.merge(self.pid, lane['id'], expected_head=lane['head'], expected_live=live)
        current_live = self.lanes._head(self.root)
        with self.assertRaisesRegex(Problem, 'Restack the lane'):
            self.integrate.merge(self.pid, lane['id'], expected_head=lane['head'], expected_live=current_live)
        self.assertEqual(self.lanes._head(Path(lane['path'])), lane['head'])
        self.assertFalse((self.root / 'saved.txt').exists())

    def test_lane_write_checks_expected_content_and_preserves_external_edit(self):
        lane = self.lanes.create(self.pid, 'Editor')
        work = Path(lane['path'])
        old = (work / 'README.md').read_text()
        (work / 'README.md').write_text('External edit\n')
        with self.assertRaisesRegex(Problem, 'changed outside'):
            self.lanes.write(self.pid, lane['id'], 'README.md', 'Stale browser edit\n', old)
        self.assertEqual((work / 'README.md').read_text(), 'External edit\n')
        self.lanes.write(self.pid, lane['id'], 'README.md', 'Fresh edit\n', 'External edit\n')
        self.assertEqual((work / 'README.md').read_text(), 'Fresh edit\n')
        self.assertFalse(list(work.glob('.unforge-write-*')))
        with self.assertRaisesRegex(Problem, 'metadata'):
            self.lanes.write(self.pid, lane['id'], '.unforge/project.json', '{}')

    def conflicts_fixture(self, *, deleted=False):
        a = self.lanes.create(self.pid, 'Alpha')
        b = self.lanes.create(self.pid, 'Beta')
        self.lanes.write(self.pid, a['id'], 'README.md', 'Alpha\n')
        self.lanes.save(self.pid, a['id'], 'Alpha readme')
        if deleted:
            (Path(b['path']) / 'README.md').unlink()
        else:
            self.lanes.write(self.pid, b['id'], 'README.md', 'Beta\n')
        self.lanes.save(self.pid, b['id'], 'Beta readme')
        self.integrate.merge(self.pid, a['id'])
        conflict = self.integrate.restack(self.pid, b['id'])
        self.assertEqual(conflict['status'], 'conflict')
        self.assertTrue(self.engine.git(Path(b['path']), 'ls-files', '-u'))
        return b

    def test_restack_theirs_resolves_real_stages_and_finishes_merge(self):
        lane = self.conflicts_fixture()
        document = self.integrate.conflicts(self.pid, lane['id'])
        self.assertEqual(document['files'][0]['ours'], 'Beta\n')
        self.assertEqual(document['files'][0]['theirs'], 'Alpha\n')
        result = self.integrate.resolve(self.pid, lane['id'], 'README.md', 'theirs', expected_revision=document['revision'])
        self.assertEqual(result['status'], 'open')
        self.assertFalse(self.engine.git(Path(lane['path']), 'ls-files', '-u'))
        self.assertEqual((Path(lane['path']) / 'README.md').read_text(), 'Alpha\n')
        self.integrate.merge(self.pid, lane['id'])
        self.assertEqual((self.root / 'README.md').read_text(), 'Alpha\n')

    def test_restack_edited_content_rejects_stale_revision_then_merges(self):
        lane = self.conflicts_fixture()
        document = self.integrate.conflicts(self.pid, lane['id'])
        with self.assertRaisesRegex(Problem, 'conflict changed'):
            self.integrate.resolve(self.pid, lane['id'], 'README.md', 'edit', 'Combined\n', expected_revision='stale')
        current = document['files'][0]['current']
        result = self.integrate.resolve(self.pid, lane['id'], 'README.md', 'edit', 'Combined\n', current, document['revision'])
        self.assertEqual(result['status'], 'open')
        self.integrate.merge(self.pid, lane['id'])
        self.assertEqual((self.root / 'README.md').read_text(), 'Combined\n')

    def test_delete_modify_conflict_can_choose_deleted_ours(self):
        lane = self.conflicts_fixture(deleted=True)
        self.assertIsNone(self.integrate.conflicts(self.pid, lane['id'])['files'][0]['ours'])
        self.integrate.resolve(self.pid, lane['id'], 'README.md', 'ours')
        self.integrate.merge(self.pid, lane['id'])
        self.assertFalse((self.root / 'README.md').exists())

    def test_child_starts_at_actual_external_parent_head(self):
        parent = self.lanes.create(self.pid, 'Parent')
        actual = self.commit_external(parent)
        child = self.lanes.create(self.pid, 'Child', parent=parent['id'])
        self.assertEqual(child['base'], actual)
        self.assertTrue((Path(child['path']) / 'external.txt').exists())

    def test_production_ref_lane_preserves_registered_checkout(self):
        production = self.lanes._head(self.root)
        self.engine.git(self.root, 'update-ref', 'refs/fixture/production', production)
        self.engine.edit(self.pid, 'registered.txt', 'Registered checkout\n')
        live = self.engine.save(self.pid, 'Working registration')['history'][0]['id']
        lane = self.lanes.create(self.pid, 'Production candidate', base_ref='refs/fixture/production')
        self.assertEqual(lane['head'], production)
        self.assertEqual(lane['baseRef'], 'refs/fixture/production')
        self.assertEqual(self.lanes._head(self.root), live)
        self.assertTrue((self.root / 'registered.txt').exists())
        self.assertFalse((Path(lane['path']) / 'registered.txt').exists())
        with self.assertRaises(Problem):
            self.lanes.create(self.pid, 'Bad ref', base_ref='HEAD~1')
        with self.assertRaises(Problem):
            self.lanes.create(self.pid, 'Both', parent=lane['id'], base_ref='refs/fixture/production')

    def test_preserved_raw_production_ref_gets_only_local_managed_identity(self):
        # Use a separate ordinary Git branch to represent preserved pre-adoption
        # source history, with no Unforge-managed identity at that revision.
        self.engine.git(self.root, 'checkout', '-b', 'raw-production')
        self.engine.git(self.root, 'rm', '--', '.unforge/project.json')
        self.engine.git(self.root, 'commit', '-m', 'Raw production source')
        raw = self.lanes._head(self.root)
        self.engine.git(self.root, 'update-ref', 'refs/fixture/production', raw)
        self.engine.git(self.root, 'checkout', 'main')
        registered = self.lanes._head(self.root)
        lane = self.lanes.create(self.pid, 'Production', base_ref='refs/fixture/production')
        self.assertEqual(lane['base'], raw)
        self.assertNotEqual(lane['head'], raw)
        work = Path(lane['path'])
        changed = self.engine.git(work, 'diff', '--name-only', raw, lane['head']).decode().splitlines()
        self.assertEqual(changed, ['.unforge/project.json'])
        self.assertEqual(json.loads((work / '.unforge/project.json').read_text()), self.engine.metadata(self.root))
        self.assertEqual(self.lanes._head(self.root), registered)
        self.assertEqual(self.engine.git(self.root, 'rev-parse', 'refs/fixture/production').decode().strip(), raw)

    def test_close_preserves_unsaved_files(self):
        lane = self.lanes.create(self.pid, 'Unsaved')
        self.lanes.write(self.pid, lane['id'], 'unsaved.txt', 'Preserve\n')
        with self.assertRaisesRegex(Problem, 'unsaved files have been preserved'):
            self.lanes.close(self.pid, lane['id'])
        self.assertEqual((Path(lane['path']) / 'unsaved.txt').read_text(), 'Preserve\n')

    def archive(self, members):
        path = self.folder / 'fixture.unforge-change'
        with tarfile.open(path, 'w:gz') as archive:
            for name, data, kind in members:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.size = len(data) if kind == tarfile.REGTYPE else 0
                if kind == tarfile.SYMTYPE:
                    member.linkname = 'ignored-target'
                archive.addfile(member, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
        return path

    def test_exchange_rejects_non_allowlisted_names_without_extraction(self):
        archive = self.archive([('../outside.txt', b'Disposable fixture', tarfile.REGTYPE)])
        before = self.engine.git(self.root, 'show-ref')
        with patch.object(tarfile.TarFile, 'extractall', side_effect=AssertionError('No extraction')):
            with self.assertRaises(Problem):
                Exchange(self.engine).import_change(self.pid, str(archive))
        self.assertEqual(self.engine.git(self.root, 'show-ref'), before)
        self.assertEqual(self.lanes.list(self.pid)['lanes'], [])
        self.assertFalse((self.folder / 'outside.txt').exists())

    def test_exchange_bounds_expansion_before_parsing_tar_headers(self):
        archive = self.folder / 'expanded.unforge-change'
        archive.write_bytes(gzip.compress(b'\0' * 70000))
        with patch('exchange.LIMIT', 512), patch('exchange.MAX_TEXT', 512):
            with self.assertRaisesRegex(Problem, 'expanded size limit'):
                Exchange(self.engine).import_change(self.pid, str(archive))
        self.assertEqual(self.lanes.list(self.pid)['lanes'], [])

    def test_exchange_rejects_links_and_duplicate_members(self):
        cases = [
            [('change.bundle', b'', tarfile.SYMTYPE)],
            [('manifest.json', b'{}', tarfile.REGTYPE), ('manifest.json', b'{}', tarfile.REGTYPE)],
        ]
        for members in cases:
            with self.subTest(members=members):
                archive = self.archive(members)
                with self.assertRaises(Problem):
                    Exchange(self.engine).import_change(self.pid, str(archive))
                self.assertEqual(self.lanes.list(self.pid)['lanes'], [])


if __name__ == '__main__':
    unittest.main()
