"""Exact lane review packets and claimed-path coordination."""
from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from care import Care
from checks import Checks
from engine import Engine, Problem, Server
from lane_review import LaneReview, MAX_CHANGED_PATHS, _bounded_text
from lanes import Lanes
from operating import Operating
from unforge import Client


class LaneReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='unforge-lane-review-')
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name) / 'home')
        self.pid = self.engine.create('Review fixture')['id']
        self.root = self.engine.root(self.pid)
        self.engine.git(self.root, 'update-ref', 'refs/fixture/production', 'HEAD')
        self.engine.edit(self.pid, '.unforge/app.json', json.dumps({
            'schemaVersion': 1,
            'productionRef': 'refs/fixture/production',
            'checks': {'schemaVersion': 1, 'jobs': [
                {'id': 'ok', 'command': ['python3', '-c', 'print("ok")']},
            ]},
        }) + '\n')
        self.engine.save(self.pid, 'Configure review fixture')
        self.lanes = Lanes(self.engine)
        self.checks = Checks(self.engine)
        self.reviewer = LaneReview(
            self.engine, self.lanes, self.checks, Care(self.engine), Operating(self.engine))

    def lane_with_changes(self, claims=None):
        lane = self.lanes.create(self.pid, 'Candidate', claimed_paths=claims)
        self.lanes.write(self.pid, lane['id'], 'src/inside.py', 'inside = True\n')
        self.lanes.write(self.pid, lane['id'], 'outside.txt', 'outside\n')
        return self.lanes.save(self.pid, lane['id'], 'Candidate changes')

    def test_claim_validation_rejects_ambiguous_or_unsafe_paths(self):
        invalid = [
            ['/absolute'], ['../outside'], ['./inside'], ['src/../outside'], ['src//file'],
            ['src\\file'], ['src/*.py'], ['src/\nfile'], ['same', 'same'], ['/'],
        ]
        for claims in invalid:
            with self.subTest(claims=claims), self.assertRaises(Problem):
                self.lanes.create(self.pid, 'Invalid', claimed_paths=claims)

    def test_claim_conflicts_warn_without_blocking_creation(self):
        first = self.lanes.create(self.pid, 'Frontend', claimed_paths=['src/', 'package.json'])
        second = self.lanes.create(self.pid, 'Button', claimed_paths=['src/button.jsx'])
        listed = {lane['id']: lane for lane in self.lanes.list(self.pid)['lanes']}
        self.assertEqual(listed[first['id']]['claimConflicts'][0]['laneId'], second['id'])
        self.assertEqual(listed[second['id']]['claimConflicts'][0]['laneId'], first['id'])
        third = self.lanes.create(self.pid, 'Docs', claimed_paths=['docs/'])
        self.assertEqual(self.lanes.get(self.pid, third['id'])['claimConflicts'], [])

    def test_review_is_exact_bounded_and_does_not_run_checks(self):
        lane = self.lane_with_changes(['src/'])
        with patch.object(self.checks, 'run', side_effect=AssertionError('review started checks')):
            first = self.reviewer.review(self.pid, lane['id'])
            second = self.reviewer.review(self.pid, lane['id'])
        self.assertEqual(first['head'], lane['head'])
        self.assertEqual(first['liveHead'], self.lanes._head(self.root))
        self.assertEqual(first['mergeBase'], first['base'])
        self.assertEqual(first['checks']['status'], 'not-checked')
        self.assertEqual(first['outOfClaimPaths'], ['outside.txt'])
        self.assertEqual(first['reviewDigest'], second['reviewDigest'])
        self.assertEqual(first['production']['relation']['kind'], 'ahead')
        self.assertIn('src/inside.py', first['diff'])
        self.assertIsNotNone(first['consequences'])
        self.assertIsNone(first['consequenceError'])
        self.checks.run(self.pid, lane_id=lane['id'], tree=lane['head'])
        checked = self.reviewer.review(self.pid, lane['id'])
        self.assertEqual(checked['checks']['status'], 'passed')
        self.assertNotEqual(checked['reviewDigest'], first['reviewDigest'])

    def test_review_bounds_path_and_diff_output(self):
        lane = self.lane_with_changes(['claimed/'])
        many = [{'status': 'A', 'path': f'outside/{index}.txt'} for index in range(MAX_CHANGED_PATHS + 3)]
        with patch.object(self.reviewer, '_changed_paths', return_value=many):
            review = self.reviewer.review(self.pid, lane['id'])
        self.assertEqual(len(review['changedPaths']), MAX_CHANGED_PATHS)
        self.assertEqual(review['changedPathCount'], MAX_CHANGED_PATHS + 3)
        self.assertTrue(review['changedPathsTruncated'])
        self.assertEqual(review['outOfClaimPathCount'], MAX_CHANGED_PATHS + 3)
        text, truncated = _bounded_text(b'abcdef', 3)
        self.assertEqual(text, 'abc')
        self.assertTrue(truncated)

    def test_consequence_failure_keeps_the_rest_of_the_review(self):
        lane = self.lane_with_changes()

        class FailingCare:
            def consequences(self, *_args):
                raise Problem('fixture consequence failure')

        reviewer = LaneReview(self.engine, self.lanes, self.checks, FailingCare(), Operating(self.engine))
        review = reviewer.review(self.pid, lane['id'])
        self.assertEqual(review['head'], lane['head'])
        self.assertIsNone(review['consequences'])
        self.assertEqual(review['consequenceError'], 'fixture consequence failure')


class LaneReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='unforge-lane-review-api-')
        self.addCleanup(self.temp.cleanup)
        self.server = Server(('127.0.0.1', 0), Engine(Path(self.temp.name) / 'workspace'), self.temp.name)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = Client(self.server.server_port)

    def test_review_route_binds_the_merge_payload(self):
        project = self.client.call('/projects', {'name': 'Review API fixture'})
        pid = project['id']
        self.server.engine.edit(pid, '.unforge/app.json', json.dumps({
            'schemaVersion': 1,
            'checks': {'schemaVersion': 1, 'jobs': [
                {'id': 'ok', 'command': ['python3', '-c', 'print("ok")']},
            ]},
        }) + '\n')
        self.server.engine.save(pid, 'Configure checks')
        lane = self.client.call(f'/projects/{pid}/lanes', {
            'name': 'Reviewed API lane', 'claimedPaths': ['src/'],
        })
        self.client.call(f'/projects/{pid}/lanes/{lane["id"]}/file', {
            'path': 'src/api.py', 'content': 'ready = True\n',
        })
        lane = self.client.call(f'/projects/{pid}/lanes/{lane["id"]}/save', {'message': 'Add API file'})
        review = self.client.call(f'/projects/{pid}/lanes/{lane["id"]}/review')
        self.assertEqual(review['head'], lane['head'])
        self.assertEqual(review['changedPaths'][0]['path'], 'src/api.py')
        with self.assertRaisesRegex(ValueError, 'review changed'):
            self.client.call(f'/projects/{pid}/lanes/{lane["id"]}/merge', {
                'expectedHead': review['head'], 'expectedLive': review['liveHead'],
                'expectedReviewDigest': '0' * 64,
            })
        self.client.call(f'/projects/{pid}/lanes/{lane["id"]}/merge', {
            'expectedHead': review['head'], 'expectedLive': review['liveHead'],
            'expectedReviewDigest': review['reviewDigest'],
        })
        self.assertEqual((self.server.engine.root(pid) / 'src' / 'api.py').read_text(), 'ready = True\n')


if __name__ == '__main__':
    unittest.main()
