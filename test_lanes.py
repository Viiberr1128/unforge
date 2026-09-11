"""Worktrees, stacked merge, collision restack, checks, and observed local live ship."""
import json
import tempfile
import unittest
from pathlib import Path

from app_manifest import AppManifest
from checks import Checks
from engine import Engine, Problem
from integrate import Integrate
from lanes import Lanes
from releases import Releases


class LaneMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='unforge-lanes-')
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name) / 'home')
        self.lanes = Lanes(self.engine)
        self.integrate = Integrate(self.engine, self.lanes)
        self.checks = Checks(self.engine)
        self.releases = Releases(self.engine, self.checks)
        self.manifest = AppManifest(self.engine, self.checks, self.releases, self.lanes)
        self.project = self.engine.create('Garden', 'Lane fixture')
        self.pid = self.project['id']
        self.root = self.engine.root(self.pid)
        self.engine.edit(self.pid, '.unforge/app.json', json.dumps({
            'schemaVersion': 1,
            'checks': {'schemaVersion': 1, 'jobs': [{'id': 'ok', 'command': ['python3', '-c', 'print("ok")']}]},
            'destinations': [{'id': 'live', 'type': 'local', 'path': str(Path(self.temp.name) / 'live-site')}],
        }) + '\n')
        self.engine.save(self.pid, 'Bind local live destination')

    def test_two_lanes_commit_without_touching_live(self):
        a = self.lanes.create(self.pid, 'Alpha')
        b = self.lanes.create(self.pid, 'Beta')
        self.lanes.write(self.pid, a['id'], 'alpha.txt', 'A\n')
        self.lanes.save(self.pid, a['id'], 'Add alpha')
        self.lanes.write(self.pid, b['id'], 'beta.txt', 'B\n')
        self.lanes.save(self.pid, b['id'], 'Add beta')
        live = self.engine.detail(self.pid)
        self.assertFalse(live['dirty'])
        self.assertFalse((self.root / 'alpha.txt').exists())
        self.assertFalse((self.root / 'beta.txt').exists())
        self.assertTrue((Path(a['path']) / 'alpha.txt').is_file())
        self.assertTrue((Path(b['path']) / 'beta.txt').is_file())

    def test_disjoint_lanes_both_merge(self):
        a = self.lanes.create(self.pid, 'Alpha')
        b = self.lanes.create(self.pid, 'Beta')
        self.lanes.write(self.pid, a['id'], 'alpha.txt', 'A\n')
        self.lanes.save(self.pid, a['id'], 'Add alpha')
        self.lanes.write(self.pid, b['id'], 'beta.txt', 'B\n')
        self.lanes.save(self.pid, b['id'], 'Add beta')
        self.integrate.merge(self.pid, a['id'])
        self.integrate.merge(self.pid, b['id'])
        self.assertEqual((self.root / 'alpha.txt').read_text(), 'A\n')
        self.assertEqual((self.root / 'beta.txt').read_text(), 'B\n')
        self.assertFalse(self.engine.detail(self.pid)['dirty'])
        self.assertEqual(self.lanes.get(self.pid, a['id'])['status'], 'merged')
        self.assertEqual(self.lanes.get(self.pid, b['id'])['status'], 'merged')

    def test_overlapping_lane_must_restack_instead_of_overwriting(self):
        a = self.lanes.create(self.pid, 'Alpha')
        b = self.lanes.create(self.pid, 'Beta')
        self.lanes.write(self.pid, a['id'], 'README.md', 'Alpha README\n')
        self.lanes.save(self.pid, a['id'], 'Alpha readme')
        self.lanes.write(self.pid, b['id'], 'README.md', 'Beta README\n')
        self.lanes.save(self.pid, b['id'], 'Beta readme')
        self.integrate.merge(self.pid, a['id'])
        self.assertEqual((self.root / 'README.md').read_text(), 'Alpha README\n')
        with self.assertRaisesRegex(Problem, 'Restack'):
            self.integrate.merge(self.pid, b['id'])
        self.assertEqual((self.root / 'README.md').read_text(), 'Alpha README\n')
        self.assertEqual(self.lanes.get(self.pid, b['id'])['status'], 'needs_restack')

    def test_stacked_child_waits_then_restacks_after_parent(self):
        parent = self.lanes.create(self.pid, 'Parent')
        self.lanes.write(self.pid, parent['id'], 'parent.txt', 'P\n')
        self.lanes.save(self.pid, parent['id'], 'Parent file')
        child = self.lanes.create(self.pid, 'Child', parent=parent['id'])
        self.lanes.write(self.pid, child['id'], 'child.txt', 'C\n')
        self.lanes.save(self.pid, child['id'], 'Child file')
        with self.assertRaisesRegex(Problem, 'parent lane first'):
            self.integrate.merge(self.pid, child['id'])
        self.integrate.merge(self.pid, parent['id'])
        self.assertEqual(self.lanes.get(self.pid, child['id'])['status'], 'needs_restack')
        self.integrate.restack(self.pid, child['id'])
        self.integrate.merge(self.pid, child['id'])
        self.assertEqual((self.root / 'parent.txt').read_text(), 'P\n')
        self.assertEqual((self.root / 'child.txt').read_text(), 'C\n')

    def test_lane_survives_engine_reopen(self):
        lane = self.lanes.create(self.pid, 'Keep')
        self.lanes.write(self.pid, lane['id'], 'keep.txt', 'still here\n')
        self.lanes.save(self.pid, lane['id'], 'Keep file')
        again = Lanes(Engine(self.engine.home))
        loaded = again.get(self.pid, lane['id'])
        self.assertEqual(loaded['name'], 'Keep')
        self.assertEqual((Path(loaded['path']) / 'keep.txt').read_text(), 'still here\n')

    def test_checks_cache_and_gate_publish(self):
        first = self.checks.run(self.pid)
        self.assertEqual(first['status'], 'passed')
        self.assertFalse(first['reused'])
        second = self.checks.run(self.pid)
        self.assertTrue(second['reused'])
        receipt = self.releases.publish(self.pid, 'live')
        self.assertTrue(receipt['observed'])
        marker = Path(self.temp.name) / 'live-site' / 'UNFORGE_RELEASE'
        self.assertEqual(marker.read_text().strip(), self.engine.git(self.root, 'rev-parse', 'HEAD').decode().strip())

    def test_github_absence_is_computed_and_cannot_be_asserted(self):
        status = self.manifest.get(self.pid)
        self.assertFalse(status['githubAbsent'])
        self.assertTrue(any('live publish' in item or 'check receipt' in item for item in status['blockers']))
        with self.assertRaisesRegex(Problem, 'cannot be set by hand'):
            self.manifest.save(self.pid, {'schemaVersion': 1, 'githubAbsent': True})
        self.checks.run(self.pid)
        self.releases.publish(self.pid, 'live')
        status = self.manifest.get(self.pid)
        self.assertTrue(status['githubAbsent'], status['blockers'])
        self.engine.git(self.root, 'remote', 'add', 'origin', 'https://github.com/example/garden.git')
        status = self.manifest.get(self.pid)
        self.assertFalse(status['githubAbsent'])
        self.assertTrue(any('GitHub remote' in item for item in status['blockers']))

    def test_full_loop_two_stacked_lanes_then_live_site(self):
        first = self.lanes.create(self.pid, 'Feature one')
        self.lanes.write(self.pid, first['id'], 'feature.txt', 'one\n')
        self.lanes.save(self.pid, first['id'], 'Feature one')
        stacked = self.lanes.create(self.pid, 'Feature two', parent=first['id'])
        self.lanes.write(self.pid, stacked['id'], 'stacked.txt', 'two\n')
        self.lanes.save(self.pid, stacked['id'], 'Feature two')
        self.integrate.merge(self.pid, first['id'])
        self.integrate.restack(self.pid, stacked['id'])
        self.integrate.merge(self.pid, stacked['id'])
        self.checks.run(self.pid)
        self.releases.publish(self.pid, 'live')
        self.assertEqual((self.root / 'feature.txt').read_text(), 'one\n')
        self.assertEqual((self.root / 'stacked.txt').read_text(), 'two\n')
        self.assertTrue(self.manifest.get(self.pid)['githubAbsent'])
        self.assertEqual(
            (Path(self.temp.name) / 'live-site' / 'UNFORGE_RELEASE').read_text().strip(),
            self.engine.git(self.root, 'rev-parse', 'HEAD').decode().strip())


if __name__ == '__main__':
    unittest.main()
