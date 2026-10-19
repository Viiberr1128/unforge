"""Relocated recovery must preserve lane work without touching original Git state."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from backups import Backups
from engine import Engine, Problem
from integrate import Integrate
from lanes import Lanes
from recovery_links import normalize_recovered_workspace


class LaneRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='unforge-lane-recovery-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.engine = Engine(self.base / 'original')
        self.pid = self.engine.create('Relocation fixture')['id']
        self.root = self.engine.root(self.pid)
        self.lanes = Lanes(self.engine)
        self.backups = Backups(self.engine)

    def lane(self):
        lane = self.lanes.create(self.pid, 'Recovered work')
        self.lanes.write(self.pid, lane['id'], 'saved.txt', 'Saved lane source\n')
        self.lanes.save(self.pid, lane['id'], 'Saved lane source')
        self.lanes.write(self.pid, lane['id'], 'draft.txt', 'Unsaved lane source\n')
        return lane

    def capture(self):
        staging = self.base / 'staging'
        staging.mkdir()
        self.backups._copy_workspace(staging)
        Backups.verify_workspace(staging)
        return staging, self.base / 'recovered'

    def promote(self, staging, target):
        report = normalize_recovered_workspace(staging, target)
        staging.rename(target)
        Backups.verify_workspace(target)
        restored = Engine(target)
        return report, restored, Lanes(restored)

    def test_unrepaired_copy_never_follows_original_lane_path(self):
        lane = self.lane(); lid = lane['id']
        target = self.base / 'raw-copy'
        shutil.copytree(self.engine.home, target)
        restored = Lanes(Engine(target))
        original = (Path(lane['path']) / 'draft.txt').read_bytes()
        with self.assertRaisesRegex(Problem, 'Git link leaves'):
            restored.write(self.pid, lid, 'draft.txt', 'Incorrect old path\n')
        self.assertEqual((Path(lane['path']) / 'draft.txt').read_bytes(), original)
        self.assertEqual((target / '.lanes' / self.pid / lid / 'draft.txt').read_bytes(), original)

    def test_saved_unsaved_history_and_verified_inventory_survive_relocation(self):
        lane = self.lane(); lid = lane['id']; old = Path(lane['path'])
        old_head = self.engine.git(old, 'rev-parse', 'HEAD')
        staging, target = self.capture()
        marker = (staging / 'UNFORGE-RECOVERY.json').read_bytes()
        report, restored, lanes = self.promote(staging, target)
        work = target / '.lanes' / self.pid / lid
        self.assertEqual(report['managedLanesRelocated'], 1)
        self.assertEqual((target / report['captureInventoryPath']).read_bytes(), marker)
        self.assertEqual(report['verifiedCaptureManifestSha256'], hashlib.sha256(marker).hexdigest())
        self.assertEqual(lanes.get(self.pid, lid)['path'], str(work))
        self.assertEqual(restored.git(work, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip(), str(target / self.pid / '.git'))
        lanes.write(self.pid, lid, 'draft.txt', 'Recovered new edit\n')
        lanes.save(self.pid, lid, 'Saved only in recovered workspace')
        self.assertEqual((old / 'draft.txt').read_text(), 'Unsaved lane source\n')
        self.assertEqual(self.engine.git(old, 'rev-parse', 'HEAD'), old_head)
        self.assertEqual((work / 'draft.txt').read_text(), 'Recovered new edit\n')

    def test_recovery_is_independent_when_original_workspace_is_lost(self):
        lane = self.lane(); lid = lane['id']
        staging, target = self.capture()
        shutil.rmtree(self.engine.home)
        report, restored, lanes = self.promote(staging, target)
        self.assertEqual(report['managedLanesRelocated'], 1)
        lanes.save(self.pid, lid, 'Save recovered draft without original workspace')
        self.assertFalse(lanes.get(self.pid, lid)['dirty'])
        self.assertEqual((target / '.lanes' / self.pid / lid / 'draft.txt').read_text(), 'Unsaved lane source\n')

    def test_pending_conflict_index_and_resolution_survive_relocation(self):
        first = self.lanes.create(self.pid, 'First')
        second = self.lanes.create(self.pid, 'Conflict')
        self.lanes.write(self.pid, first['id'], 'README.md', 'First version\n')
        self.lanes.save(self.pid, first['id'], 'First version')
        self.lanes.write(self.pid, second['id'], 'README.md', 'Second version\n')
        self.lanes.save(self.pid, second['id'], 'Second version')
        self.engine.git(self.root, 'merge', '--no-edit', self.lanes._head(Path(first['path'])))
        integrate = Integrate(self.engine, self.lanes)
        self.assertEqual(integrate.restack(self.pid, second['id'])['status'], 'conflict')
        original_work = Path(second['path'])
        original_unmerged = self.engine.git(original_work, 'ls-files', '-u')
        staging, target = self.capture()
        _, restored, lanes = self.promote(staging, target)
        work = target / '.lanes' / self.pid / second['id']
        self.assertEqual(restored.git(work, 'ls-files', '-u'), original_unmerged)
        recovered_integrate = Integrate(restored, lanes)
        recovered_integrate.resolve(self.pid, second['id'], 'README.md', 'ours')
        self.assertEqual(lanes.get(self.pid, second['id'])['status'], 'open')
        self.assertEqual(self.engine.git(original_work, 'ls-files', '-u'), original_unmerged)

    def test_external_worktree_metadata_is_quarantined_and_detached_head_retained(self):
        external = self.base / 'external-owner'
        self.engine.git(self.root, 'worktree', 'add', '--detach', str(external), 'HEAD')
        external_head = self.engine.git(external, 'rev-parse', 'HEAD')
        (external / 'unsaved.txt').write_text('External owner draft\n')
        staging, target = self.capture()
        report, restored, _ = self.promote(staging, target)
        self.assertEqual(report['externalWorktreesQuarantined'], 1)
        self.assertEqual(report['detachedHeadsPreserved'], 1)
        self.assertNotIn(str(external), restored.git(restored.root(self.pid), 'worktree', 'list', '--porcelain').decode())
        self.assertEqual(len(list((target / '.recovered-git-worktrees' / self.pid).iterdir())), 1)
        kept = restored.git(restored.root(self.pid), 'for-each-ref', '--format=%(objectname)', 'refs/unforge-recovered-worktrees/').strip()
        self.assertEqual(kept, external_head.strip())
        self.assertEqual((external / 'unsaved.txt').read_text(), 'External owner draft\n')
        self.assertEqual(self.engine.git(external, 'rev-parse', 'HEAD'), external_head)

    def test_effective_worktree_configuration_and_replaced_common_dir_fail_closed(self):
        lane = self.lane(); lid = lane['id']; work = Path(lane['path'])
        self.engine.git(self.root, 'config', 'extensions.worktreeConfig', 'true')
        self.engine.git(work, 'config', '--worktree', 'core.worktree', str(self.root))
        with self.assertRaisesRegex(Problem, 'configuration leaves'):
            self.lanes.write(self.pid, lid, 'draft.txt', 'Wrong configured checkout\n')
        self.engine.git(work, 'config', '--worktree', 'core.worktree', str(work))
        staging, target = self.capture()
        _, restored, lanes = self.promote(staging, target)
        lanes.write(self.pid, lid, 'draft.txt', 'Correct relocated configuration\n')
        admin = target / self.pid / '.git/worktrees' / lid
        (admin / 'commondir').write_text(str(self.root / '.git') + '\n')
        with self.assertRaisesRegex(Problem, 'common Git directory leaves'):
            lanes.save(self.pid, lid, 'Refuse external common directory')

    def test_unknown_managed_link_refuses_promotion_and_keeps_original(self):
        lane = self.lane(); lid = lane['id']
        staging, target = self.capture()
        (staging / '.lanes' / self.pid / lid / '.git').write_text('gitdir: /uncaptured/other/project/.git/worktrees/other\n')
        with self.assertRaisesRegex(Problem, 'not linked to its captured project'):
            normalize_recovered_workspace(staging, target)
        self.assertFalse(target.exists())
        self.assertEqual((Path(lane['path']) / 'draft.txt').read_text(), 'Unsaved lane source\n')

    def test_linked_storage_directory_and_index_never_follow_external_locations(self):
        external = self.base / 'other-owner'; external.mkdir()
        link = self.lanes.root / self.pid
        link.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(Problem, 'symbolic links'):
            self.lanes.create(self.pid, 'Unsafe redirected storage')
        self.assertEqual(list(external.iterdir()), [])
        self.assertEqual(self.engine.git(self.root, 'for-each-ref', '--format=%(refname)', 'refs/heads/unforge-lane-'), b'')
        link.unlink()
        index = external / 'index.json'; index.write_text('[]')
        (self.lanes.root / (self.pid + '.json')).symlink_to(index)
        with self.assertRaisesRegex(Problem, 'symbolic links'):
            self.lanes.list(self.pid)
        self.assertEqual(index.read_text(), '[]')

    def test_captured_config_includes_refuse_promotion_without_original_reads(self):
        lane = self.lane()
        original_work = Path(lane['path'])
        include = self.base / 'original-owner.gitconfig'
        include.write_text('[core]\n\tworktree = ' + str(original_work) + '\n')
        staging, target = self.capture()
        original_run = __import__('subprocess').run
        for declaration in ('[include]', '[includeIf "gitdir:**"]'):
            with self.subTest(declaration=declaration):
                config = staging / self.pid / '.git/config'
                source_config = (self.root / '.git/config').read_text()
                config.write_text(source_config + '\n' + declaration + '\n\tpath = ' + str(include) + '\n')
                def safe_config_run(command, **kwargs):
                    self.assertEqual(command[:2], ['git', 'config'])
                    self.assertIn('--no-includes', command)
                    self.assertIn(str(config), command)
                    return original_run(command, **kwargs)
                with patch('recovery_links.subprocess.run', side_effect=safe_config_run):
                    with self.assertRaisesRegex(Problem, 'includes uncaptured settings'):
                        normalize_recovered_workspace(staging, target)
                self.assertFalse(target.exists())
                self.assertEqual((original_work / 'draft.txt').read_text(), 'Unsaved lane source\n')
                self.assertEqual((staging / '.lanes' / self.pid / lane['id'] / '.git').read_text(), (original_work / '.git').read_text())


if __name__ == '__main__': unittest.main()
