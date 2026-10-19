"""The public Fleet bridge is exercised with a disposable local executable only."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

from agent_jobs import AgentJobs
from agent_plans import AgentPlans
from engine import Engine, Problem
from lanes import Lanes
from operations import Operations


FAKE_FLEET = r'''
import difflib
import hashlib
import json
from pathlib import Path
import sys

if sys.argv[1] != 'bridge' or sys.argv[2] not in ('health', 'run', 'recover', 'finalize'):
    raise SystemExit(2)
state_root = Path(sys.argv[0]).parent
result_path = Path(sys.argv[sys.argv.index('--result') + 1])
if sys.argv[2] == 'health':
    result_path.write_text(json.dumps({'schema_version': 1, 'bridge_protocol_versions': [1, 2],
        'fleet_version': 'fixture', 'features': {'progress': True},
        'lanes': [{'id': 'grok-build', 'state': 'ready', 'allowance_fresh': True, 'refresh_needed': []},
                  {'id': 'cursor-other', 'state': 'ready', 'allowance_fresh': True, 'refresh_needed': []}],
        'paid_api_fallback': False, 'daemon': False, 'boundary': 'Fixture health.'}))
    raise SystemExit(0)
if sys.argv[2] == 'finalize':
    operation = sys.argv[sys.argv.index('--operation-id') + 1]
    decision = sys.argv[sys.argv.index('--decision') + 1]
    result_path.write_text(json.dumps({'schema_version': 1, 'operation_id': operation, 'decision': decision,
        'finalized_at': 'fixture-time'}))
    raise SystemExit(0)
if sys.argv[2] == 'run':
    request_path = Path(sys.argv[sys.argv.index('--request') + 1])
    request = json.loads(request_path.read_text())
    root = Path(request['repo'])
    old = (root / 'README.md').read_text().splitlines(keepends=True)
    new = ['Fleet proposal\n']
    patch = 'diff --git a/README.md b/README.md\n' + ''.join(
        difflib.unified_diff(old, new, fromfile='a/README.md', tofile='b/README.md'))
    if 'bad-checksum' in request['objective']:
        digest = '0' * 64
    else:
        digest = hashlib.sha256(patch.encode()).hexdigest()
    report = 'CRITICAL: fixture defect remains.' if 'review-blocker' in request['objective'] else 'No blocking issue reported by the fixture reviewer.'
    result = {
        'schema_version': 2,
        'kind': 'candidate',
        'operation_id': request['operation_id'],
        'request_sha256': hashlib.sha256((json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()).hexdigest(),
        'job_id': 'unforge-fixture',
        'status': 'candidate-awaiting-integration',
        'base_sha': request['base_sha'],
        'patch': patch,
        'patch_sha256': digest,
        'check_sha256': request.get('check_sha256'),
        'author': {'provider': 'grok-build', 'outcome': 'ok'},
        'reviewer': {'provider': 'cursor-other', 'outcome': 'completed-uninterpreted',
                     'attention': ['Reviewer narrative requires human interpretation.'], 'report': report},
        'proof': {'status': 'passed' if request.get('check_argv') else 'not-run',
                  'claim': 'fixture focused check passed' if request.get('check_argv') else None},
        'boundary': 'No integration or deployment occurred.',
    }
    if 'bad-metadata' in request['objective']:
        result['author'] = {'provider': ['not', 'text'], 'outcome': 'ok'}
    durable = {'request_sha256': hashlib.sha256((json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()).hexdigest(),
               'result': result}
    (state_root / ('durable-' + request['operation_id'] + '.json')).write_text(json.dumps(durable))
    count = state_root / 'run-count'
    count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
    if 'result-symlink' in request['objective']:
        outside = result_path.with_name('fleet-result-outside.json')
        outside.write_text(json.dumps(result))
        result_path.symlink_to(outside)
    else:
        result_path.write_text(json.dumps(result))
else:
    operation = sys.argv[sys.argv.index('--operation-id') + 1]
    base = sys.argv[sys.argv.index('--base-sha') + 1]
    request_sha = sys.argv[sys.argv.index('--request-sha256') + 1]
    durable = json.loads((state_root / ('durable-' + operation + '.json')).read_text())
    result = durable['result']
    if durable['request_sha256'] != request_sha or result['base_sha'] != base:
        raise SystemExit(3)
    result_path.write_text(json.dumps(result))
print('Fleet candidate prepared', flush=True)
'''


class FleetBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.engine = Engine(self.base / 'home')
        self.project = self.engine.create('Fleet garden')
        self.pid = self.project['id']
        self.root = self.engine.root(self.pid)
        checks = {'checks': {'schemaVersion': 1, 'jobs': [
            {'id': 'readme-fixture', 'command': [sys.executable, '-c', 'raise SystemExit(0)']},
        ]}}
        self.engine.edit(self.pid, '.unforge/app.json', json.dumps(checks))
        self.engine.save(self.pid, 'Configure checks')
        self.fake = self.base / 'fake-fleet'
        self.fake.write_text('#!' + sys.executable + '\n' + FAKE_FLEET)
        self.fake.chmod(0o700)
        self.ledger = Operations(self.engine.home)
        self.jobs = AgentJobs(self.engine, executable=str(self.base / 'missing-codex'),
                              fleet_executable=str(self.fake), operations=self.ledger,
                              fleet_timeout_seconds=30)

    def tearDown(self):
        self.jobs.close()
        self.ledger.close()
        self.temp.cleanup()

    def finish(self, objective='Update the readme', operation='fleet-operation'):
        started = self.jobs.start(self.pid, objective, operation, 'subscription-fleet', {
            'acceptance': 'README contains only the Fleet proposal marker.',
            'allowPaths': ['README.md'],
            'size': 'small',
        })
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = self.jobs.get(started['id'])
            if result['status'] != 'running':
                return result
            time.sleep(0.02)
        self.fail('Fake Fleet did not finish')

    def test_status_exposes_optional_providers_without_authentication_claim(self):
        status = self.jobs.status()
        providers = {item['id']: item for item in status['providers']}
        self.assertFalse(providers['codex']['available'])
        self.assertTrue(providers['subscription-fleet']['available'])
        self.assertEqual(providers['subscription-fleet']['state'], 'ready')
        self.assertIn('existing subscription sign-ins', providers['subscription-fleet']['note'])

    def test_fleet_health_is_independently_bounded_and_sanitized(self):
        executable = self.base / 'extra-health-fields'
        executable.write_text('#!' + sys.executable + '\n' + r'''
import json
from pathlib import Path
import sys
result = Path(sys.argv[sys.argv.index('--result') + 1])
result.write_text(json.dumps({'schema_version':1,'bridge_protocol_versions':[1,2],
  'fleet_version':'fixture','features':{'progress':True,'invented':True},
  'lanes':[{'id':'grok-build','state':'ready','allowance_fresh':True,'refresh_needed':[],
            'unexpected_detail':'local-detail'}],
  'paid_api_fallback':False,'daemon':False,'boundary':'ignored'}))
''')
        executable.chmod(0o700)
        health = self.jobs._fleet_health(str(executable))
        self.assertEqual(health['features'], {'progress': True})
        self.assertEqual(health['lanes'], [{
            'id': 'grok-build', 'state': 'ready', 'allowance_fresh': True, 'refresh_needed': []}])
        self.assertNotIn('local-detail', json.dumps(health))
        symlink = self.base / 'symlink-health'
        symlink.write_text('#!' + sys.executable + '\n' + r'''
from pathlib import Path
import sys
result = Path(sys.argv[sys.argv.index('--result') + 1])
target = result.with_name('redirected-health.json')
target.write_text('{}')
result.symlink_to(target)
''')
        symlink.chmod(0o700)
        self.assertIsNone(self.jobs._fleet_health(str(symlink)))

    def test_base_bound_plan_starts_v2_with_focused_check(self):
        plan = AgentPlans(self.engine).create(self.pid, 'Update the readme')
        self.assertEqual(plan['status'], 'ready')
        fleet = {'acceptance': plan['acceptance'], 'allowPaths': plan['allowPaths'], 'size': plan['size'],
                 'taskClass': plan['taskClass'], 'projectContext': plan['projectContext'],
                 'focusedCheck': plan['focusedCheck']}
        started = self.jobs.start(self.pid, plan['outcome'], 'planned-operation', plan['provider'], fleet,
                                  plan_id=plan['id'])
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = self.jobs.get(started['id'])
            if result['status'] != 'running':
                break
            time.sleep(0.02)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['fleet']['proof']['status'], 'passed')
        self.assertEqual(result['planId'], plan['id'])

    def test_fleet_candidate_is_verified_materialized_and_applied_through_lane(self):
        before = (self.root / 'README.md').read_text()
        result = self.finish()
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual((self.root / 'README.md').read_text(), before)
        self.assertEqual(result['changedFiles'], [{'path': 'README.md', 'status': 'modified'}])
        self.assertEqual(result['fleet']['author']['provider'], 'grok-build')
        self.assertEqual(result['fleet']['reviewer']['provider'], 'cursor-other')
        with self.assertRaisesRegex(Problem, 'acknowledge'):
            self.jobs.apply(result['id'])
        self.jobs.apply(result['id'], review_acknowledged=True)
        self.assertEqual((self.root / 'README.md').read_text(), 'Fleet proposal\n')
        saved = self.jobs.get(result['id'])
        self.assertEqual(saved['fleetFinalization']['status'], 'recorded')
        record = json.loads((self.root / '.unforge' / 'proposals' / (result['id'] + '.json')).read_text())
        self.assertEqual(record['provider'], 'subscription-fleet')
        self.assertEqual(record['fleet']['jobId'], 'unforge-fixture')
        self.assertTrue(record['fleetReviewAcknowledged'])
        self.assertNotIn('report', record['fleet']['reviewer'])
        self.assertTrue(record['fleet']['reviewer']['reportRetainedInLocalHistory'])

    def test_operation_replay_does_not_launch_fleet_again(self):
        first = self.finish()
        self.fake.unlink()
        second = self.jobs.start(self.pid, 'Update the readme', 'fleet-operation', 'subscription-fleet', {
            'acceptance': 'README contains only the Fleet proposal marker.',
            'allowPaths': ['README.md'],
            'size': 'small',
        })
        self.assertEqual(second['id'], first['id'])
        self.assertTrue(second['replayed'])
        self.assertEqual(self.ledger.overview()['usedToday'], 1)

    def test_reviewer_success_is_preserved_as_uninterpreted_testimony(self):
        result = self.finish('review-blocker', 'review-blocker-operation')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['fleet']['reviewer']['outcome'], 'completed-uninterpreted')
        self.assertIn('CRITICAL', result['fleet']['reviewer']['report'])

    def test_reject_records_fleet_feedback_without_applying_source(self):
        before = (self.root / 'README.md').read_text()
        result = self.finish('reject this candidate', 'reject-operation')
        lane_path = Path(Lanes(self.engine).get(self.pid, result['laneId'])['path'])
        self.assertTrue(lane_path.is_dir())
        rejected = self.jobs.reject(result['id'])
        self.assertEqual(rejected['status'], 'rejected')
        self.assertEqual(rejected['fleetFinalization']['decision'], 'reject')
        self.assertTrue(rejected['laneClosed'])
        self.assertFalse(lane_path.exists())
        self.assertEqual((self.root / 'README.md').read_text(), before)

    def test_unknown_job_recovers_exact_durable_result_without_new_attempt(self):
        result = self.finish('Update the readme', 'recover-operation')
        with self.jobs._lock:
            job = self.jobs._jobs[result['id']]
            job.update(status='unknown', diff='', changedFiles=[], error=None)
            for key in ('fleet', 'proposalHead', 'proposalTree'):
                job.pop(key, None)
            self.jobs._persist(job)
        with sqlite3.connect(self.ledger.path) as database:
            database.execute("UPDATE operations SET state='unknown' WHERE operation_id=?", ('recover-operation',))
        recovered = self.jobs.recover(result['id'])
        self.assertEqual(recovered['status'], 'completed')
        self.assertEqual(recovered['changedFiles'], [{'path': 'README.md', 'status': 'modified'}])
        self.assertEqual(recovered['operationState'], 'succeeded')
        self.assertEqual((self.base / 'run-count').read_text(), '1')
        self.assertEqual(self.ledger.overview()['usedToday'], 1)

    def test_invalid_scope_is_rejected_before_allowance_or_process_launch(self):
        for paths in ([], ['README.md'] * 2, ['.unforge/project.json'], ['folder']):
            if paths == ['folder']:
                (self.root / 'folder').mkdir()
            with self.subTest(paths=paths), self.assertRaises(Problem):
                self.jobs.start(self.pid, 'Change source', 'scope-' + hashlib.sha256(repr(paths).encode()).hexdigest()[:8],
                                'subscription-fleet', {'acceptance': 'A bounded result.', 'allowPaths': paths, 'size': 'small'})
        self.assertEqual(self.ledger.overview()['usedToday'], 0)

    def test_changed_candidate_outside_allowlist_is_rejected(self):
        result = self.jobs.start(self.pid, 'Update the readme', 'wrong-path', 'subscription-fleet', {
            'acceptance': 'A different exact file changes.',
            'allowPaths': ['new-file.txt'],
            'size': 'small',
        })
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = self.jobs.get(result['id'])
            if result['status'] != 'running':
                break
            time.sleep(0.02)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('outside its exact allowance', result['error'])
        self.assertEqual((self.root / 'README.md').read_text(), '# Fleet garden\n\n\n')

    def test_tampered_fleet_patch_is_rejected(self):
        result = self.finish('bad-checksum', 'bad-checksum-operation')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('checksum', result['error'])
        self.assertFalse(result['proposalAvailable'])

    def test_unbounded_fleet_metadata_is_rejected(self):
        result = self.finish('bad-metadata', 'bad-metadata-operation')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('author metadata', result['error'])
        self.assertFalse(result['proposalAvailable'])

    def test_fleet_result_symlink_is_not_followed(self):
        result = self.finish('result-symlink', 'result-symlink-operation')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('bounded candidate artifact', result['error'])
        self.assertFalse(result['proposalAvailable'])


if __name__ == '__main__':
    unittest.main()
