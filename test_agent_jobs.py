import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_jobs import AgentJobs, MAX_LOG, VERIFICATION_CACHES, agent_environment
from engine import Engine, Problem
from operations import Operations


FAKE = r'''
import json, os, subprocess, sys, time
from pathlib import Path
request = sys.stdin.read().split('User request:\n', 1)[1].strip()
print(json.dumps({'argv': sys.argv[1:], 'request': request}), flush=True)
if request in ('sleep', 'child'):
    child = None
    if request == 'child':
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        print(json.dumps({'child': child.pid}), flush=True)
    time.sleep(60)
elif request == 'failed':
    Path('README.md').write_text('Must not reach the project')
    print('Deliberate simulated CLI failure', file=sys.stderr, flush=True)
    sys.exit(7)
elif request == 'metadata':
    Path('.unforge/project.json').write_text('{}')
elif request == 'reserved':
    Path('.env').write_text('SECRET=do-not-copy')
elif request == 'binary':
    Path('picture.bin').write_bytes(b'\x00binary')
elif request == 'oversize':
    Path('large.txt').write_text('a' * (128 * 1024 + 1))
elif request == 'symlink':
    Path('link').symlink_to('/tmp')
elif request == 'noop':
    print('No changes were needed')
elif request == 'poison-git':
    git = Path('.git')
    if git.is_dir():
        (git / 'config').write_text('not a valid git config')
    Path('README.md').write_text('Safe review clone\n')
elif request == 'directory-to-file':
    Path('folder/note.txt').unlink()
    Path('folder').rmdir()
    Path('folder').write_text('Now a file\n')
else:
    if request == 'logs':
        print('x' * (300 * 1024), flush=True)
    Path('README.md').write_text('Changed by the fake agent\n')
    Path('new file.txt').write_text('New source\n')
    Path('index.html').unlink()
'''


class AgentJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.engine = Engine(self.base / 'projects')
        self.project = self.engine.create('Garden')
        self.pid = self.project['id']
        self.root = self.engine.root(self.pid)
        self.fake = self.base / 'fake-codex'
        self.fake.write_text('#!' + sys.executable + '\n' + FAKE)
        self.fake.chmod(0o755)
        self.jobs = AgentJobs(self.engine, executable=str(self.fake), timeout_seconds=10)

    def tearDown(self):
        self.jobs.close()
        self.temp.cleanup()

    def finish(self, job):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            current = self.jobs.get(job['id'])
            if current['status'] != 'running':
                return current
            time.sleep(0.025)
        self.fail('Fake agent did not finish in time')

    def test_proposal_is_isolated_and_accepts_added_modified_deleted_files(self):
        job = self.finish(self.jobs.start(self.pid, 'change'))
        self.assertEqual(job['status'], 'completed', job['error'])
        self.assertEqual(job['exitCode'], 0)
        self.assertFalse(self.engine.detail(self.pid)['dirty'])
        self.assertEqual((self.root / 'README.md').read_text(), '# Garden\n\n\n')
        self.assertIn('Changed by the fake agent', job['diff'])
        self.assertEqual({item['status'] for item in job['changedFiles']}, {'added', 'modified', 'deleted'})
        detail = self.jobs.apply(job['id'])
        self.assertFalse(detail['dirty'])
        self.assertGreater(len(detail['history']), len(self.project['history']))
        self.assertEqual((self.root / 'new file.txt').read_text(), 'New source\n')
        self.assertFalse((self.root / 'index.html').exists())
        record = json.loads((self.root / '.unforge/proposals' / (job['id'] + '.json')).read_text())
        self.assertEqual(record['baseVersion'], job['baseVersion'])
        self.assertEqual(record['request'], 'change')
        self.assertNotIn('output', record)
        self.assertIn('not test or deployment proof', record['note'])
        self.assertEqual(self.jobs.get(job['id'])['status'], 'applied')
        with self.assertRaisesRegex(Problem, 'unapplied'):
            self.jobs.apply(job['id'])

    def test_cli_argv_is_scoped_and_prompt_uses_stdin(self):
        text = 'Make a change; $(do-not-execute) "quotes"'
        job = self.finish(self.jobs.start(self.pid, text))
        event = json.loads(job['output'].splitlines()[0])
        self.assertEqual(event['request'], text)
        self.assertNotIn(text, event['argv'])
        self.assertEqual(event['argv'][:5], ['--ask-for-approval', 'never', 'exec', '--sandbox', 'workspace-write'])
        self.assertIn('--ignore-user-config', event['argv'])
        self.assertIn('--ignore-rules', event['argv'])
        self.assertIn('--ephemeral', event['argv'])
        self.assertEqual(event['argv'][-1], '-')
        self.assertNotIn(str(self.root), event['argv'])

    def test_dirty_start_and_changed_base_apply_are_rejected(self):
        self.engine.edit(self.pid, 'README.md', 'Not saved')
        with self.assertRaisesRegex(Problem, 'Save your current'):
            self.jobs.start(self.pid, 'change')
        self.engine.save(self.pid, 'Save first')
        job = self.finish(self.jobs.start(self.pid, 'change'))
        self.engine.edit(self.pid, 'README.md', 'Newer decision')
        with self.assertRaisesRegex(Problem, 'Save your current changes before merging'):
            self.jobs.apply(job['id'])
        self.engine.save(self.pid, 'Newer version')
        with self.assertRaisesRegex(Problem, 'Restack'):
            self.jobs.apply(job['id'])
        self.assertEqual((self.root / 'README.md').read_text(), 'Newer decision')

    def test_ignored_file_collision_is_not_overwritten(self):
        self.engine.edit(self.pid, '.gitignore', 'new file.txt\n')
        self.engine.save(self.pid, 'Ignore local notes')
        job = self.finish(self.jobs.start(self.pid, 'change'))
        self.assertEqual(job['status'], 'completed', job['error'])
        self.assertIn('new file.txt', job['diff'])
        (self.root / 'new file.txt').write_text('Private ignored notes')
        with self.assertRaisesRegex(Problem, 'ignored file'):
            self.jobs.apply(job['id'])
        self.assertEqual((self.root / 'new file.txt').read_text(), 'Private ignored notes')

    def test_agent_git_config_is_never_used_for_proposal(self):
        job = self.finish(self.jobs.start(self.pid, 'poison-git'))
        self.assertEqual(job['status'], 'completed', job['error'])
        self.jobs.apply(job['id'])
        self.assertEqual((self.root / 'README.md').read_text(), 'Safe review clone\n')

    def test_directory_replaced_by_file(self):
        self.engine.edit(self.pid, 'folder/note.txt', 'Before')
        self.engine.save(self.pid, 'Add folder')
        job = self.finish(self.jobs.start(self.pid, 'directory-to-file'))
        self.assertEqual(job['status'], 'completed', job['error'])
        self.jobs.apply(job['id'])
        self.assertEqual((self.root / 'folder').read_text(), 'Now a file\n')

    def test_failed_run_does_not_offer_source_application(self):
        job = self.finish(self.jobs.start(self.pid, 'failed'))
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['exitCode'], 7)
        self.assertIn('Deliberate simulated CLI failure', job['output'])
        self.assertFalse(self.engine.detail(self.pid)['dirty'])
        with self.assertRaises(Problem):
            self.jobs.apply(job['id'])

    def test_reserved_binary_oversize_symlink_and_metadata_changes_rejected(self):
        for request in ('metadata', 'reserved', 'binary', 'oversize', 'symlink'):
            with self.subTest(request=request):
                job = self.finish(self.jobs.start(self.pid, request))
                self.assertEqual(job['status'], 'failed', job)
                self.assertTrue(job['error'])
                self.assertEqual(job['diff'], '')
                self.assertFalse(self.engine.detail(self.pid)['dirty'])

    def test_logs_are_bounded_and_pipes_do_not_deadlock(self):
        job = self.finish(self.jobs.start(self.pid, 'logs'))
        self.assertEqual(job['status'], 'completed', job['error'])
        self.assertEqual(len(job['output'].encode()), MAX_LOG)
        self.assertTrue(job['outputTruncated'])

    def test_one_running_job_across_instances_and_cancel_releases_lock(self):
        job = self.jobs.start(self.pid, 'sleep')
        second = AgentJobs(self.engine, executable=str(self.fake))
        try:
            with self.assertRaisesRegex(Problem, 'already running'):
                second.start(self.pid, 'change')
            cancelled = self.jobs.cancel(job['id'])
            self.assertEqual(cancelled['status'], 'cancelled')
            next_job = second.start(self.pid, 'noop')
            self.assertEqual(second.cancel(next_job['id'])['status'], 'cancelled')
        finally:
            second.close()

    def test_timeout_stops_parent_and_child(self):
        self.jobs.timeout_seconds = 0.5
        job = self.finish(self.jobs.start(self.pid, 'child'))
        self.assertEqual(job['status'], 'timed_out')
        child = next(json.loads(line)['child'] for line in job['output'].splitlines() if '"child"' in line and '"argv"' not in line)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = subprocess.run(['ps', '-p', str(child), '-o', 'stat='], capture_output=True, text=True)
            if not result.stdout.strip() or result.stdout.strip().startswith('Z'):
                break
            time.sleep(0.025)
        else:
            self.fail('Agent child remained alive')
        self.assertFalse(self.engine.detail(self.pid)['dirty'])

    def test_unavailable_cli_and_noop_are_honest(self):
        absent = AgentJobs(self.engine, executable=str(self.base / 'not-installed'))
        self.assertFalse(absent.status()['available'])
        with self.assertRaisesRegex(Problem, 'not installed'):
            absent.start(self.pid, 'change')
        absent.close()
        job = self.finish(self.jobs.start(self.pid, 'noop'))
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['changedFiles'], [])
        with self.assertRaisesRegex(Problem, 'did not produce'):
            self.jobs.apply(job['id'])

    def test_environment_does_not_forward_provider_credentials(self):
        with patch.dict(os.environ, {'AWS_SECRET_ACCESS_KEY': 'private', 'OPENAI_API_KEY': 'private',
                                    'GOOGLE_APPLICATION_CREDENTIALS': 'private', 'GH_TOKEN': 'private',
                                    'CODEX_HOME': '/existing/account', 'HOME': '/existing/home'}):
            env = agent_environment()
        self.assertEqual(env['CODEX_HOME'], '/existing/account')
        self.assertEqual(env['HOME'], '/existing/home')
        self.assertFalse({'AWS_SECRET_ACCESS_KEY', 'OPENAI_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS', 'GH_TOKEN'} & env.keys())

    def test_project_runtime_config_is_not_loaded(self):
        self.engine.edit(self.pid, '.codex/config.toml', 'model = "unexpected"\n')
        self.engine.save(self.pid, 'Add runtime config')
        with self.assertRaisesRegex(Problem, 'runtime configuration'):
            self.jobs.start(self.pid, 'change')

    def test_case_variant_project_config_is_not_loaded(self):
        self.engine.edit(self.pid, '.CODEX/config.TOML', 'model = "unexpected"\n')
        self.engine.save(self.pid, 'Add case variant runtime config')
        # This request must fail before any CLI starts. A private no-op lock keeps
        # this validation-only test independent of a real live agent job.
        with tempfile.TemporaryFile() as private_lock, patch.object(self.jobs, '_execution_lock', return_value=private_lock):
            with self.assertRaisesRegex(Problem, 'runtime configuration'):
                self.jobs.start(self.pid, 'change')

    def test_new_verification_caches_are_omitted_from_source_snapshot(self):
        baseline = self.jobs._snapshot(self.root)
        for name in VERIFICATION_CACHES:
            folder = self.root / 'nested' / name
            folder.mkdir(parents=True)
            (folder / 'artifact.bin').write_bytes(b'\0generated-cache')
        (self.root / 'README.md').write_text('Actual source improvement\n')
        after = self.jobs._snapshot(self.root, baseline=baseline)
        self.assertEqual(set(after), set(baseline))
        self.assertNotEqual(after['README.md'], baseline['README.md'])

    def test_tracked_cache_contents_are_not_omitted(self):
        folder = self.root / '__pycache__'
        folder.mkdir()
        (folder / 'owned.txt').write_text('Existing source')
        baseline = self.jobs._snapshot(self.root)
        (folder / 'owned.txt').write_text('A source change')
        (folder / 'new.bin').write_bytes(b'\0new')
        after = self.jobs._snapshot(self.root, baseline=baseline)
        self.assertNotEqual(after['__pycache__/owned.txt'], baseline['__pycache__/owned.txt'])
        self.assertIn('__pycache__/new.bin', after)

    def test_cache_symlink_and_reserved_source_paths_are_still_rejected(self):
        baseline = self.jobs._snapshot(self.root)
        link = self.root / '__pycache__'
        link.symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(Problem, 'Symbolic links'):
            self.jobs._snapshot(self.root, baseline=baseline)
        link.unlink()
        folder = self.root / '__pycache__'
        folder.mkdir()
        (folder / 'cache.bin').write_bytes(b'\0cache')
        (self.root / '.env').write_text('PRIVATE=not-for-proposal')
        with self.assertRaisesRegex(Problem, 'reserved'):
            self.jobs._snapshot(self.root, baseline=baseline)

    def test_recent_job_status_discovers_latest_project_proposals(self):
        records = {str(index): dict(id=str(index), projectId=self.pid, status='completed', output='private log')
                   for index in range(35)}
        with patch.object(self.jobs, '_jobs', records):
            recent = self.jobs.status()['recentJobs']
        self.assertEqual(len(recent), 32)
        self.assertEqual(recent[0], dict(id='34', projectId=self.pid, status='completed'))
        self.assertEqual(recent[-1]['id'], '3')
        self.assertTrue(all(set(item) == {'id', 'projectId', 'status'} for item in recent))

    def test_returned_proposal_cannot_mutate_internal_acceptance(self):
        job = self.finish(self.jobs.start(self.pid, 'change'))
        job['changedFiles'][0]['path'] = '../outside'
        self.assertNotEqual(self.jobs.get(job['id'])['changedFiles'][0]['path'], '../outside')

    def test_close_cancels_running_job_and_disallows_new_work(self):
        job = self.jobs.start(self.pid, 'sleep')
        self.jobs.close()
        self.assertEqual(self.jobs.get(job['id'])['status'], 'cancelled')
        with self.assertRaisesRegex(Problem, 'closed'):
            self.jobs.start(self.pid, 'change')


class AgentAllowanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.engine = Engine(self.base / 'projects')
        self.project = self.engine.create('Garden')
        self.pid = self.project['id']
        self.root = self.engine.root(self.pid)
        self.fake = self.base / 'fake-codex'
        self.fake.write_text('#!' + sys.executable + '\n' + FAKE)
        self.fake.chmod(0o755)
        self.ledger = Operations(self.engine.home)
        self.jobs = AgentJobs(self.engine, executable=str(self.fake), timeout_seconds=10, operations=self.ledger)

    def tearDown(self):
        self.jobs.close()
        self.ledger.close()
        self.temp.cleanup()

    finish = AgentJobsTests.finish

    def test_known_replay_returns_same_proposal_without_consuming_allowance(self):
        first = self.finish(self.jobs.start(self.pid, 'change', 'request-1'))
        second = self.jobs.start(self.pid, 'change', 'request-1')
        self.assertTrue(second['replayed'])
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(second['diff'], first['diff'])
        self.assertEqual(self.ledger.overview()['usedToday'], 1)
        self.assertEqual(self.ledger.get('request-1')['state'], 'succeeded')
        with self.assertRaisesRegex(Problem, 'already bound'):
            self.jobs.start(self.pid, 'different request', 'request-1')

    def test_replay_while_running_returns_same_job(self):
        first = self.jobs.start(self.pid, 'sleep', 'request-1')
        replay = self.jobs.start(self.pid, 'sleep', 'request-1')
        self.assertEqual(replay['id'], first['id'])
        self.assertEqual(replay['status'], 'running')
        self.assertTrue(replay['replayed'])
        self.jobs.cancel(first['id'])
        self.assertEqual(self.ledger.overview()['usedToday'], 1)

    def test_budget_is_shared_between_projects_and_persists(self):
        self.ledger.configure(1, 1)
        self.finish(self.jobs.start(self.pid, 'change', 'request-1'))
        other = self.engine.create('Other')
        with self.assertRaisesRegex(Problem, 'allowance reached'):
            self.jobs.start(other['id'], 'change', 'request-2')
        self.assertEqual(self.ledger.overview()['usedToday'], 1)
        self.assertEqual(len(self.jobs.status()['recentJobs']), 1)

    def test_three_empty_attempts_pause_and_explicit_resume_allows_next(self):
        for index in range(3):
            job = self.finish(self.jobs.start(self.pid, 'noop', 'empty-' + str(index)))
            self.assertEqual(job['status'], 'completed')
            self.assertEqual(job['operationState'], 'empty')
        with self.assertRaisesRegex(Problem, 'paused'):
            self.jobs.start(self.pid, 'change', 'after-empty')
        self.ledger.resume(self.pid)
        job = self.finish(self.jobs.start(self.pid, 'change', 'after-empty'))
        self.assertEqual(job['operationState'], 'succeeded')

    def test_preflight_errors_do_not_spend_an_attempt(self):
        with self.assertRaises(Problem):
            self.jobs.start(self.pid, '', 'invalid')
        self.engine.edit(self.pid, 'README.md', 'Not saved')
        with self.assertRaisesRegex(Problem, 'Save your current'):
            self.jobs.start(self.pid, 'change', 'dirty')
        self.engine.save(self.pid, 'Save')
        self.jobs.executable = str(self.base / 'missing')
        with self.assertRaisesRegex(Problem, 'not installed'):
            self.jobs.start(self.pid, 'change', 'missing')
        self.jobs.executable = str(self.fake)
        self.engine.edit(self.pid, '.codex/config.toml', 'model="unexpected"')
        self.engine.save(self.pid, 'Config')
        with self.assertRaisesRegex(Problem, 'runtime configuration'):
            self.jobs.start(self.pid, 'change', 'config')
        self.assertEqual(self.ledger.overview()['usedToday'], 0)

    def test_restart_returns_saved_proposal_even_without_cli_or_with_dirty_project(self):
        first = self.finish(self.jobs.start(self.pid, 'change', 'request-1'))
        self.jobs.close()
        self.ledger.close()
        self.ledger = Operations(self.engine.home)
        self.jobs = AgentJobs(self.engine, executable=str(self.base / 'missing'), operations=self.ledger)
        self.engine.edit(self.pid, 'README.md', 'New unsaved work')
        replay = self.jobs.start(self.pid, 'change', 'request-1')
        self.assertEqual(replay['id'], first['id'])
        self.assertEqual(replay['status'], 'completed')
        self.assertEqual(replay['operationState'], 'succeeded')
        self.assertTrue(replay['proposalAvailable'])
        self.assertEqual(replay['diff'], first['diff'])
        self.assertEqual(replay['output'], first['output'])
        self.assertEqual(replay['recordedResult']['changedFileCount'], 3)
        self.assertEqual(self.jobs.get(first['id']), replay)
        self.assertEqual(self.jobs.status()['recentJobs'][0]['id'], first['id'])
        self.assertEqual(self.ledger.overview()['usedToday'], 1)

    def test_unknown_interrupted_attempt_never_invokes_cli_again(self):
        abandoned = Operations(self.engine.home)
        abandoned.reserve(self.pid, 'interrupted', 'agent', {'request': 'change'}, reference_id='a' * 32,
                          initial_result={'jobStatus': 'running', 'baseVersion': self.project['history'][0]['id']})
        abandoned.close()
        with patch.object(self.jobs, '_command', side_effect=AssertionError('Replay must not reach CLI preflight')):
            replay = self.jobs.start(self.pid, 'change', 'interrupted')
        self.assertEqual(replay['status'], 'unknown')
        self.assertEqual(replay['operationState'], 'unknown')
        self.assertTrue(replay['replayed'])
        self.assertFalse(self.engine.detail(self.pid)['dirty'])
        self.assertEqual(self.ledger.overview()['usedToday'], 1)

    def test_thread_launch_failure_is_recorded_and_execution_lock_is_released(self):
        with patch('threading.Thread.start', side_effect=RuntimeError('Cannot start thread')):
            with self.assertRaisesRegex(RuntimeError, 'Cannot start'):
                self.jobs.start(self.pid, 'change', 'thread-error')
        self.assertEqual(self.ledger.get('thread-error')['state'], 'failed')
        self.assertEqual(self.jobs.start(self.pid, 'change', 'thread-error')['status'], 'archived')
        job = self.finish(self.jobs.start(self.pid, 'change', 'next'))
        self.assertEqual(job['status'], 'completed', job['error'])


if __name__ == '__main__':
    unittest.main()
