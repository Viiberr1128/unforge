"""Result-level checks/release regressions; all projects and providers are disposable."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_manifest import AppManifest
from checks import Checks, MAX_OUTPUT
from engine import Engine, Problem
from github_import import GitHubImport
from lanes import Lanes
from releases import Releases
from runtime import heavy_lock


class CheckReleaseRepairs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='unforge-check-release-test-')
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name) / 'home')
        self.pid = self.engine.create('Fixture')['id']
        self.root = self.engine.root(self.pid)
        self.checks = Checks(self.engine)
        self.releases = Releases(self.engine, self.checks)
        self.live = Path(self.temp.name) / 'live'

    def configure(self, jobs=None, destinations=None):
        app = {'schemaVersion': 1, 'checks': {'schemaVersion': 1, 'jobs': jobs or [
            {'id': 'check', 'command': [sys.executable, '-c', 'print("checked")']}]},
            'destinations': destinations or [{'id': 'live', 'type': 'local', 'path': str(self.live)}]}
        self.engine.edit(self.pid, '.unforge/app.json', json.dumps(app))
        self.engine.save(self.pid, 'Configure meaningful checks')
        return app

    def head(self):
        return self.engine.git(self.root, 'rev-parse', 'HEAD').decode().strip()

    def test_missing_checks_does_not_pass_or_publish(self):
        self.assertEqual(self.checks.run(self.pid)['status'], 'not-configured')
        self.assertFalse(self.checks.passed(self.pid, self.head()))
        self.releases.bind(self.pid, {'id': 'live', 'type': 'local', 'path': str(self.live)})
        self.engine.save(self.pid, 'Bind only')
        with self.assertRaisesRegex(Problem, 'Checks must pass'):
            self.releases.publish(self.pid, 'live')
        self.assertFalse(self.live.exists())

    def test_runtime_fallback_prepares_then_checks_in_configured_directory(self):
        (self.root / 'web').mkdir()
        (self.root / 'web/input.txt').write_text('source')
        runtime = {'schemaVersion': 1, 'directory': 'web',
                   'prepare': [sys.executable, '-c', 'from pathlib import Path; Path("prepared.txt").write_text(Path("input.txt").read_text())'],
                   'check': [sys.executable, '-c', 'from pathlib import Path; assert Path("prepared.txt").read_text()=="source"']}
        self.engine.edit(self.pid, '.unforge/runtime.json', json.dumps(runtime))
        self.engine.save(self.pid, 'Runtime fallback')
        receipt = self.checks.run(self.pid)
        self.assertEqual(receipt['status'], 'passed')
        self.assertEqual([job['id'] for job in receipt['jobs']], ['prepare', 'check'])
        self.assertFalse((self.root / 'web/prepared.txt').exists())

    def test_lane_configuration_and_actual_external_head_are_checked(self):
        app = self.configure()
        lanes = Lanes(self.engine)
        lane = lanes.create(self.pid, 'Change checks')
        original = lane['head']
        app['checks']['jobs'][0]['command'] = [sys.executable, '-c', 'raise SystemExit(9)']
        work = Path(lane['path'])
        (work / '.unforge/app.json').write_text(json.dumps(app))
        self.engine.git(work, 'add', '.unforge/app.json')
        self.engine.git(work, 'commit', '-m', 'External failing check')
        actual = self.engine.git(work, 'rev-parse', 'HEAD').decode().strip()
        receipt = self.checks.run(self.pid, lane_id=lane['id'])
        self.assertNotEqual(actual, original)
        self.assertEqual(receipt['tree'], actual)
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['jobs'][0]['exitCode'], 9)

    def test_checks_keep_immutable_input_during_external_edit(self):
        (self.root / 'data.txt').write_text('original')
        command = f'from pathlib import Path; Path({str(self.root / "data.txt")!r}).write_text("external"); assert Path("data.txt").read_text()=="original"'
        self.configure([{'id': 'input', 'command': [sys.executable, '-c', command]}])
        original = self.head()
        receipt = self.checks.run(self.pid)
        self.assertEqual(receipt['status'], 'passed')
        self.assertEqual(receipt['tree'], original)
        self.assertEqual((self.root / 'data.txt').read_text(), 'external')

    def test_checks_cannot_certify_source_they_changed(self):
        (self.root / 'data.txt').write_text('original')
        self.configure([{'id': 'mutation', 'command': [sys.executable, '-c', 'from pathlib import Path; Path("data.txt").write_text("changed")']}])
        receipt = self.checks.run(self.pid)
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('changed saved source', receipt['error'])
        self.assertEqual((self.root / 'data.txt').read_text(), 'original')

    def test_cache_false_runs_fresh_graph_including_dependency_preparations(self):
        cached_counter = Path(self.temp.name) / 'cached'
        fresh_counter = Path(self.temp.name) / 'fresh'
        def counter(path):
            return [sys.executable, '-c', f'from pathlib import Path; p=Path({str(path)!r}); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")']
        self.configure([{'id': 'source', 'command': counter(cached_counter)},
                        {'id': 'health', 'command': counter(fresh_counter), 'cache': False}])
        self.assertEqual(self.checks.run(self.pid)['status'], 'passed')
        second = self.checks.run(self.pid)
        self.assertEqual(second['status'], 'passed')
        self.assertFalse(second['jobs'][0]['reused'])
        self.assertFalse(second['jobs'][1]['reused'])
        self.assertEqual(cached_counter.read_text(), '2')
        self.assertEqual(fresh_counter.read_text(), '2')
        self.assertFalse(self.checks.passed(self.pid, self.head()))

    def test_output_is_bounded_while_streaming(self):
        self.configure([{'id': 'output', 'command': [sys.executable, '-c', 'import sys; sys.stdout.write("a"*1000000)']}])
        receipt = self.checks.run(self.pid)
        self.assertEqual(receipt['status'], 'passed')
        self.assertTrue(receipt['jobs'][0]['outputTruncated'])
        self.assertEqual(len(receipt['jobs'][0]['output']), MAX_OUTPUT)

    def test_timeout_reaps_the_owned_descendant_group(self):
        pid_file = Path(self.temp.name) / 'child.pid'
        command = f'import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(60)'
        self.configure([{'id': 'timeout', 'command': [sys.executable, '-c', command], 'timeoutSeconds': 1}])
        receipt = self.checks.run(self.pid)
        self.assertEqual(receipt['status'], 'failed')
        self.assertTrue(receipt['jobs'][0]['timedOut'])
        child_pid = int(pid_file.read_text())
        inspection = subprocess.run(['ps', '-o', 'stat=', '-p', str(child_pid)], capture_output=True, text=True)
        self.assertTrue(not inspection.stdout.strip() or inspection.stdout.strip().startswith('Z'), inspection.stdout)

    def test_checks_share_runtime_machine_wide_gate(self):
        self.configure()
        lease = heavy_lock()
        try:
            with self.assertRaisesRegex(Problem, 'Another local check'):
                Checks(self.engine).run(self.pid)
        finally:
            os.close(lease)
        self.assertEqual(self.checks.run(self.pid)['status'], 'passed')

    def test_local_publish_copies_contents_and_rollback_restores_them(self):
        self.configure()
        (self.root / 'index.html').write_text('OLD CONTENT')
        self.engine.save(self.pid, 'Old page')
        old = self.head()
        self.releases.publish(self.pid, 'live')
        self.assertEqual((self.live / 'index.html').read_text(), 'OLD CONTENT')
        (self.root / 'index.html').write_text('NEW CONTENT')
        (self.root / 'new-only.txt').write_text('new')
        self.engine.save(self.pid, 'New page')
        self.releases.publish(self.pid, 'live')
        self.assertTrue((self.live / 'new-only.txt').exists())
        receipt = self.releases.rollback(self.pid, 'live')
        self.assertEqual(receipt['tree'], old)
        self.assertEqual((self.live / 'index.html').read_text(), 'OLD CONTENT')
        self.assertFalse((self.live / 'new-only.txt').exists())
        self.assertEqual((self.live / 'UNFORGE_RELEASE').read_text().strip(), old)
        self.assertEqual((self.root / 'index.html').read_text(), 'NEW CONTENT')

    def test_checked_ignored_build_is_retained_not_current_dist(self):
        (self.root / '.gitignore').write_text('dist/\n')
        command = 'from pathlib import Path; p=Path("dist"); p.mkdir(); (p/"index.html").write_text(Path("input.txt").read_text())'
        self.configure([{'id': 'build', 'command': [sys.executable, '-c', command]}], [
            {'id': 'live', 'type': 'local', 'path': str(self.live), 'directory': 'dist'}])
        (self.root / 'input.txt').write_text('checked old')
        self.engine.save(self.pid, 'Build old')
        self.checks.run(self.pid)
        (self.root / 'dist').mkdir()
        (self.root / 'dist/index.html').write_text('unrelated mutable build')
        self.releases.publish(self.pid, 'live')
        self.assertEqual((self.live / 'index.html').read_text(), 'checked old')

    def test_manifest_does_not_claim_historical_release_for_new_source_or_binding(self):
        self.configure()
        self.releases.publish(self.pid, 'live')
        old = self.head()
        (self.root / 'index.html').write_text('new')
        self.engine.save(self.pid, 'New source')
        self.checks.run(self.pid)
        manifest = AppManifest(self.engine, self.checks, self.releases)
        status = manifest.get(self.pid)
        self.assertFalse(status['githubAbsent'])
        self.assertEqual(status['sourceVersion'], self.head())
        self.assertEqual(status['liveVersion'], old)
        self.releases.bind(self.pid, {'id': 'live', 'type': 'local', 'path': str(Path(self.temp.name) / 'other')})
        self.assertIsNone(self.releases.last_observed(self.pid))

    def test_newer_rollback_cannot_reuse_historical_publication_as_current_proof(self):
        self.configure()
        first = self.head()
        self.releases.publish(self.pid, 'live')
        (self.root / 'index.html').write_text('Second source')
        self.engine.save(self.pid, 'Second source')
        second = self.head()
        self.releases.publish(self.pid, 'live')
        self.assertTrue(AppManifest(self.engine, self.checks, self.releases).get(self.pid)['githubAbsent'])
        self.releases.rollback(self.pid, 'live')
        self.releases.observe(self.pid, 'live', tree=first)
        self.assertIsNone(self.releases.last_observed(self.pid, tree=second))
        self.assertEqual(self.releases.last_observed(self.pid)['tree'], first)
        status = AppManifest(self.engine, self.checks, self.releases).get(self.pid)
        self.assertEqual(status['sourceVersion'], second)
        self.assertEqual(status['liveVersion'], first)
        self.assertFalse(status['githubAbsent'])
        self.assertIn('Observe the live site for this source version.', status['blockers'])

    def test_failed_explicit_observation_invalidates_older_successful_proof(self):
        self.configure()
        self.releases.publish(self.pid, 'live')
        (self.live / 'UNFORGE_RELEASE').unlink()
        with self.assertRaisesRegex(Problem, 'does not identify'):
            self.releases.observe(self.pid, 'live')
        self.assertFalse(self.releases.history(self.pid)[-1]['observed'])
        self.assertIsNone(self.releases.last_observed(self.pid))
        status = AppManifest(self.engine, self.checks, self.releases).get(self.pid)
        self.assertFalse(status['githubAbsent'])
        self.assertIsNone(status['liveVersion'])

    def test_provider_command_receives_exact_candidate_cwd(self):
        self.configure()
        (self.root / 'supabase/functions/demo').mkdir(parents=True)
        (self.root / 'supabase/functions/demo/index.ts').write_text('exact saved function')
        self.engine.save(self.pid, 'Function')
        dest = {'id': 'functions', 'type': 'supabase-functions', 'projectRef': 'abcdefghijabcdefghij', 'url': 'https://example.com'}
        self.releases.bind(self.pid, dest)
        self.engine.save(self.pid, 'Bind functions')
        seen = []
        def runner(args, env):
            cwd = Path(env['UNFORGE_COMMAND_CWD'])
            seen.append((cwd / 'supabase/functions/demo/index.ts').read_text())
            return subprocess.CompletedProcess(args, 0)
        releases = Releases(self.engine, self.checks, runner)
        with patch.object(releases, '_observe_http', return_value={'kind': 'fake-http'}):
            releases.publish(self.pid, 'functions')
        self.assertEqual(seen, ['exact saved function'])

    def test_fly_observation_retains_identity_when_readiness_is_unhealthy(self):
        self.configure(destinations=[{'id': 'fly', 'type': 'fly', 'app': 'owned-app', 'readinessUrl': 'https://example.com/ready'}])
        head = self.head()
        data = {'ID': 'owned-app', 'Version': 5, 'Machines': [{'id': 'machine', 'state': 'started',
                'image_ref': {'digest': 'sha256:' + 'a' * 64, 'labels': {'org.opencontainers.image.revision': head}}}]}
        def runner(args, env):
            self.assertEqual(args[1:3], ['status', '--app'])
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(data))
        releases = Releases(self.engine, self.checks, runner)
        with patch.object(releases, '_readiness', return_value={'ok': False, 'error': 'HTTP503', 'observedAt': 'now'}):
            observed = releases.observe(self.pid, 'fly')
        self.assertEqual(observed['tree'], head)
        self.assertFalse(observed['readiness']['ok'])
        data['Machines'][0]['image_ref']['labels']['org.opencontainers.image.revision'] = '0' * 40
        with self.assertRaisesRegex(Problem, 'does not match'):
            releases.observe(self.pid, 'fly')

    def test_fly_optional_source_tree_label_is_destination_owned(self):
        label = 'example.source-tree'
        self.configure(destinations=[{'id': 'fly', 'type': 'fly', 'app': 'owned-app', 'sourceTreeLabel': label}])
        head = self.head()
        expected_tree = self.engine.git(self.root, 'rev-parse', head + '^{tree}').decode().strip()
        data = {'ID': 'owned-app', 'Machines': [{'id': 'machine', 'state': 'started',
                'image_ref': {'digest': 'sha256:' + 'a' * 64, 'labels': {
                    'org.opencontainers.image.revision': head, label: expected_tree}}}]}
        releases = Releases(self.engine, self.checks, lambda args, env: subprocess.CompletedProcess(args, 0, stdout=json.dumps(data)))
        self.assertEqual(releases.observe(self.pid, 'fly')['tree'], head)
        data['Machines'][0]['image_ref']['labels'][label] = '0' * 40
        with self.assertRaisesRegex(Problem, 'does not match'):
            releases.observe(self.pid, 'fly')
        with self.assertRaisesRegex(Problem, 'OCI label keys'):
            releases.validate_destination({'id': 'fly', 'type': 'fly', 'app': 'owned-app', 'sourceTreeLabel': '../bad label'})

    def test_worker_observation_requires_current_deployment_full_revision(self):
        self.configure(destinations=[{'id': 'worker', 'type': 'cloudflare-worker', 'worker': 'owned-worker', 'profile': 'owned'}])
        deployments = [{'id': 'deployment', 'created_on': 'today', 'versions': [{'version_id': 'version', 'percentage': 100}]}]
        versions = [{'id': 'version', 'annotations': {'workers/message': 'Source ' + self.head()}}]
        def runner(args, env):
            self.assertIn('owned', args)
            data = deployments if args[1] == 'deployments' else versions
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(data))
        releases = Releases(self.engine, self.checks, runner)
        self.assertEqual(releases.observe(self.pid, 'worker')['evidence']['deployment'], 'deployment')
        versions[0]['annotations']['workers/message'] = 'source revision unavailable'
        with self.assertRaisesRegex(Problem, 'no matching full Git revision'):
            releases.observe(self.pid, 'worker')
        versions[0]['annotations']['workers/message'] = 'Previous ' + self.head() + ' current ' + '0' * 40
        with self.assertRaisesRegex(Problem, 'no matching full Git revision'):
            releases.observe(self.pid, 'worker')

    def test_multiple_fly_machines_cannot_hide_a_mixed_revision(self):
        self.configure(destinations=[{'id': 'fly', 'type': 'fly', 'app': 'owned-app'}])
        def machine(revision):
            return {'id': revision[:8], 'state': 'started', 'image_ref': {'digest': 'sha256:' + 'a' * 64,
                    'labels': {'org.opencontainers.image.revision': revision}}}
        data = {'ID': 'owned-app', 'Machines': [machine(self.head()), machine('0' * 40)]}
        releases = Releases(self.engine, self.checks, lambda args, env: subprocess.CompletedProcess(args, 0, stdout=json.dumps(data)))
        with self.assertRaisesRegex(Problem, 'does not match'):
            releases.observe(self.pid, 'fly')

    def test_real_command_uses_cwd_and_returns_usable_json_stdout(self):
        value = self.releases._run([sys.executable, '-c', 'import json; from pathlib import Path; print(json.dumps({"cwd":str(Path.cwd())}))'], cwd=self.root)
        self.assertEqual(json.loads(value.stdout)['cwd'], str(self.root.resolve()))

    def test_generated_artifact_retention_preserves_previous_rollback_contents(self):
        self.configure()
        records = []
        for value in ['one', 'two', 'three', 'four']:
            (self.root / 'index.html').write_text(value)
            self.engine.save(self.pid, value)
            records.append(self.releases.publish(self.pid, 'live'))
        store = self.releases.root / self.pid / 'artifacts'
        self.assertEqual(len(list(store.iterdir())), 2)
        self.assertFalse(Path(records[0]['artifactPath']).exists())
        self.releases.rollback(self.pid, 'live')
        self.assertEqual((self.live / 'index.html').read_text(), 'three')

    def test_local_publish_preserves_unrecorded_destination_edits(self):
        self.configure()
        self.releases.publish(self.pid, 'live')
        (self.live / 'manual.txt').write_text('Preserve me')
        (self.root / 'index.html').write_text('new')
        self.engine.save(self.pid, 'New source')
        with self.assertRaisesRegex(Problem, 'changed outside Unforge'):
            self.releases.publish(self.pid, 'live')
        self.assertEqual((self.live / 'manual.txt').read_text(), 'Preserve me')

    def test_readiness_http503_is_a_recorded_failure_not_an_exception(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"status":"unready"}')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        value = self.releases._readiness({'readinessUrl': f'http://127.0.0.1:{server.server_port}/'}, self.head())
        self.assertFalse(value['ok'])
        self.assertEqual(value['httpStatus'], 503)

    def test_empty_actions_import_does_not_invent_success(self):
        result = GitHubImport(self.engine).import_workflows(self.pid)
        self.assertIsNone(result['checks'])
        self.engine.save(self.pid, 'Record import')
        self.assertEqual(self.checks.run(self.pid)['status'], 'not-configured')


if __name__ == '__main__':
    unittest.main()
