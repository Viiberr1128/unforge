import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_plans import AgentPlans
from engine import Engine, Problem


class AgentPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(Path(self.temp.name) / 'home')
        self.project = self.engine.create('Plan garden')
        self.pid = self.project['id']
        self.root = self.engine.root(self.pid)
        self.engine.edit(self.pid, 'src/readmeView.js', 'export const readmeView = "old";\n')
        self.engine.edit(self.pid, 'src/readmeView.test.js', 'test("readme", () => {});\n')
        self.engine.edit(self.pid, '.unforge/app.json', json.dumps({'checks': {'schemaVersion': 1, 'jobs': [
            {'id': 'readme', 'command': ['node', '--test', 'src/readmeView.test.js'], 'directory': '.', 'timeoutSeconds': 45},
        ]}}))
        self.engine.save(self.pid, 'Add plan fixture')
        self.plans = AgentPlans(self.engine)

    def tearDown(self):
        self.temp.cleanup()

    def test_plan_is_read_only_bounded_and_base_bound(self):
        before = self.engine.git(self.root, 'status', '--porcelain')
        plan = self.plans.create(self.pid, 'Update the readme view')
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(before, self.engine.git(self.root, 'status', '--porcelain'))
        self.assertIn('src/readmeView.js', plan['allowPaths'])
        self.assertLessEqual(len(plan['allowPaths']), 8)
        self.assertEqual(plan['focusedCheck']['id'], 'readme')
        self.assertEqual(self.plans.get(plan['id'])['digest'], plan['digest'])

    def test_stale_and_cross_project_plan_are_rejected(self):
        plan = self.plans.create(self.pid, 'Update the readme view')
        other = self.engine.create('Other project')['id']
        with self.assertRaisesRegex(Problem, 'not ready for this project'):
            self.plans.resolve(other, plan['id'])
        self.engine.edit(self.pid, 'README.md', 'Changed\n')
        self.engine.save(self.pid, 'Move source')
        with self.assertRaisesRegex(Problem, 'changed after this plan'):
            self.plans.resolve(self.pid, plan['id'])

    def test_unbounded_outcome_asks_one_question(self):
        plan = self.plans.create(self.pid, 'Improve xyqzplugh')
        self.assertEqual(plan['status'], 'needs_input')
        self.assertEqual(plan['allowPaths'], [])
        self.assertTrue(plan['question'].endswith('?'))

    def test_content_search_finds_relevant_file_late_in_large_tree(self):
        bulk = self.root / 'src' / 'bulk'
        bulk.mkdir()
        for index in range(305):
            (bulk / f'a{index:03}.js').write_text('export const ordinary = true;\n')
        target = self.root / 'src' / 'z-last-feature.js'
        target.write_text('export const lanternworkflow = true;\n')
        self.engine.git(self.root, 'add', '--', 'src/bulk', 'src/z-last-feature.js')
        self.engine.git(self.root, 'commit', '-m', 'Add large planning fixture')
        plan = self.plans.create(self.pid, 'Improve lanternworkflow behavior')
        self.assertIn('src/z-last-feature.js', plan['allowPaths'])

    def test_started_plan_cannot_run_twice_and_is_pruned_before_ready_work(self):
        first = self.plans.create(self.pid, 'Update the readme view')
        claimed = self.plans.claim(self.pid, first['id'], 'operation-1')
        self.assertEqual(claimed['status'], 'starting')
        self.assertEqual(self.plans.claim(self.pid, first['id'], 'operation-1')['status'], 'starting')
        with self.assertRaisesRegex(Problem, 'not ready'):
            self.plans.claim(self.pid, first['id'], 'operation-2')
        self.plans.mark_started(self.pid, first['id'], 'a' * 32, 'operation-1')
        with self.assertRaisesRegex(Problem, 'not ready'):
            self.plans.resolve(self.pid, first['id'])
        replay = self.plans.resolve(self.pid, first['id'], 'operation-1')
        self.assertEqual(replay['jobId'], 'a' * 32)
        with patch('agent_plans.MAX_PLANS', 2):
            second = self.plans.create(self.pid, 'Update the readme view')
            third = self.plans.create(self.pid, 'Update the readme view')
        with self.assertRaisesRegex(Problem, 'Unknown agent plan'):
            self.plans.get(first['id'])
        self.assertEqual(self.plans.get(second['id'])['status'], 'ready')
        self.assertEqual(self.plans.get(third['id'])['status'], 'ready')

    def test_failed_start_releases_exact_plan_claim(self):
        plan = self.plans.create(self.pid, 'Update the readme view')
        self.plans.claim(self.pid, plan['id'], 'operation-failed')
        released = self.plans.release_claim(self.pid, plan['id'], 'operation-failed')
        self.assertEqual(released['status'], 'ready')
        self.assertNotIn('operationId', released)
        self.assertEqual(self.plans.resolve(self.pid, plan['id'])['id'], plan['id'])


if __name__ == '__main__':
    unittest.main()
