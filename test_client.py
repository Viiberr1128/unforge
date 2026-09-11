import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from engine import Engine, Server
from unforge import Client, main


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.server = Server(('127.0.0.1', 0), Engine(self.home / 'projects'), self.home / 'static')
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = Client(self.server.server_port)

    def test_independent_client_can_create_edit_export_and_reconstruct(self):
        project = self.client.call('/projects', {'name': 'Portable project', 'description': 'No account needed'})
        prefix = '/projects/' + project['id']
        original = next(f['content'] for f in project['files'] if f['path'] == 'README.md')
        self.client.call(prefix + '/file', {'path': 'README.md', 'content': '# Owned\n', 'expectedContent': original})
        saved = self.client.call(prefix + '/save', {'message': 'Make it mine'})
        bundle = self.home / 'portable.bundle'
        bundle.write_bytes(self.client.call(prefix + '/export', binary=True))
        clone = self.home / 'independent-copy'
        subprocess.run(['git', 'clone', str(bundle), str(clone)], capture_output=True, check=True)
        self.assertEqual((clone / 'README.md').read_text(), '# Owned\n')
        cloned_head = subprocess.check_output(['git', '-C', str(clone), 'rev-parse', 'HEAD']).decode().strip()
        self.assertEqual(cloned_head, saved['history'][0]['id'])
        self.assertTrue((clone / '.unforge/project.json').is_file())
        imported = self.client.call('/import', {'path': str(bundle), 'name': 'My independent copy'})
        self.assertNotEqual(imported['id'], project['id'])
        self.assertEqual(imported['name'], 'My independent copy')
        self.assertIn(saved['history'][0]['id'], [item['id'] for item in imported['history']])
        report = self.client.call('/projects/' + imported['id'] + '/insights')
        self.assertEqual(report['services'], [])
        self.assertEqual(len(report['architectures']), 3)

    def test_cli_returns_json_and_refuses_overwriting_an_export(self):
        project = self.client.call('/projects', {'name': 'CLI example'})
        target = self.home / 'existing.bundle'
        target.write_text('keep this')
        with patch('sys.argv', ['unforge.py', '--port', str(self.server.server_port), 'export', project['id'], '--output', str(target)]):
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                code = main()
        self.assertEqual(code, 1)
        self.assertIn('error', json.loads(errors.getvalue()))
        self.assertEqual(target.read_text(), 'keep this')

    def test_empty_new_file_precondition_does_not_replace_an_existing_file(self):
        project = self.client.call('/projects', {'name': 'Prevent overwrite'})
        with self.assertRaises(ValueError):
            self.client.call('/projects/' + project['id'] + '/file', {'path': 'README.md', 'content': '', 'expectedContent': None})
        created = self.client.call('/projects/' + project['id'] + '/file', {'path': 'new.md', 'content': 'New document', 'expectedContent': None})
        self.assertTrue(any(f['path'] == 'new.md' for f in created['files']))

    def cli(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['unforge.py', '--port', str(self.server.server_port), *arguments]):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = main()
        return code, json.loads(output.getvalue()) if output.getvalue() else None, errors.getvalue()

    def test_cli_care_requires_exact_revision_and_exports_portable_handoff(self):
        project = self.client.call('/projects', {'name': 'Care from CLI'})
        pid = project['id']
        code, care, _ = self.cli('care', pid)
        self.assertEqual(code, 0)
        self.assertIsNone(care['revision'])
        document = care['document']
        document['brief']['purpose'] = 'Keep my gardening notes useful offline.'
        source = self.home / 'care.json'
        source.write_text(json.dumps(document))
        code, saved, _ = self.cli('care-save', pid, '--from-file', str(source), '--revision', 'null')
        self.assertEqual(code, 0)
        self.assertEqual(saved['document']['brief']['purpose'], document['brief']['purpose'])
        code, _, errors = self.cli('care-save', pid, '--from-file', str(source), '--revision', 'null')
        self.assertEqual(code, 1)
        self.assertIn('changed', json.loads(errors)['error'])
        code, checked, _ = self.cli('behavior-check', pid, '--revision', saved['revision'], '--example', 'Notes remain after reopening', '--outcome', 'pass', '--note', 'Saved a note, reopened the project, and saw the same text.')
        self.assertEqual(code, 0)
        self.assertEqual(checked['document']['checks'][0]['outcome'], 'pass')
        self.assertEqual(self.cli('consequences', pid)[0], 0)
        code, simplified, _ = self.cli('simplify', pid)
        self.assertEqual(code, 0)
        self.assertIn(document['brief']['purpose'], simplified['request'])
        destination = self.home / 'handoff.md'
        code, receipt, _ = self.cli('handoff', pid, '--output', str(destination))
        self.assertEqual(code, 0)
        self.assertEqual(receipt['bytes'], destination.stat().st_size)
        self.assertIn(document['brief']['purpose'], destination.read_text())
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        original = destination.read_bytes()
        self.assertEqual(self.cli('handoff', pid, '--output', str(destination))[0], 1)
        self.assertEqual(destination.read_bytes(), original)

    def test_cli_recovery_captures_rehearses_downloads_imports_and_retires(self):
        project = self.client.call('/projects', {'name': 'Recovery from CLI'})
        pid = project['id']
        prefix = '/projects/' + pid
        self.client.call(prefix + '/file', {'path': '.gitignore', 'content': 'uploads/\n'})
        self.client.call(prefix + '/save', {'message': 'Separate source and local data'})
        root = self.server.engine.root(pid)
        (root / 'uploads').mkdir()
        (root / 'uploads' / 'note.txt').write_text('Keep this local attachment')
        assets = self.home / 'assets.json'
        assets.write_text(json.dumps([{'path': 'uploads', 'kind': 'directory'}]))
        code, capsule, errors = self.cli('recovery-create', pid, '--assets-file', str(assets))
        self.assertEqual(code, 0, errors)
        self.assertFalse(capsule['sourceOnly'])
        code, rehearsal, errors = self.cli('recovery-rehearse', pid, capsule['id'])
        self.assertEqual(code, 0, errors)
        self.assertTrue(rehearsal['ok'])
        state = self.cli('recovery', pid)[1]
        self.assertEqual(state['rehearsals'][0]['capsuleId'], capsule['id'])
        destination = self.home / 'capsule.tar.gz'
        code, receipt, _ = self.cli('recovery-download', pid, capsule['id'], '--output', str(destination))
        self.assertEqual(code, 0)
        self.assertEqual(receipt['bytes'], destination.stat().st_size)
        for command in (('recovery-import', str(destination)), ('recovery-restore', pid, capsule['id'])):
            code, restored, errors = self.cli(*command)
            self.assertEqual(code, 0, errors)
            self.assertNotEqual(restored['id'], pid)
            self.assertEqual((self.server.engine.root(restored['id']) / 'uploads' / 'note.txt').read_text(), 'Keep this local attachment')
        self.assertTrue(self.cli('retirement', pid)[1]['canComplete'])
        code, retired, errors = self.cli('retire', pid, capsule['id'], '--revision', 'null', '--note', 'Rehearsal checked; this local-only example has no known external resources.')
        self.assertEqual(code, 0, errors)
        self.assertEqual(retired['document']['retirement']['state'], 'complete')
        self.assertTrue(root.exists())

    def test_cli_allowance_practice_replay_reconcile_and_resume(self):
        project = self.client.call('/projects', {'name': 'Operations from CLI'})
        pid = project['id']
        payload = self.home / 'practice.json'
        payload.write_text(json.dumps({'example': 'No real payment'}))
        args = ('practice', pid, 'payment', '--operation-id', 'payment-once', '--from-file', str(payload))
        code, first, _ = self.cli(*args)
        self.assertEqual(code, 0)
        self.assertTrue(first['result']['simulated'])
        code, current, _ = self.cli('operations')
        self.assertEqual(code, 0)
        self.assertEqual(current['usedToday'], 1)
        revision = str(current['settings']['revision'])
        self.assertEqual(self.cli('allowance', '0', '--revision', revision)[0], 0)
        self.assertEqual(self.cli('allowance', '20', '--revision', revision)[0], 1)
        code, repeated, _ = self.cli(*args)
        self.assertEqual(code, 0)
        self.assertTrue(repeated['replayed'])
        self.assertEqual(first['result'], repeated['result'])
        self.assertEqual(self.cli('allowance', '20', '--revision', '2')[0], 0)
        self.server.operations.reserve(pid, 'uncertain', 'agent', {})
        self.server.operations.finish('uncertain', 'unknown', {'message': 'Lost result'})
        code, result, _ = self.cli('reconcile', 'uncertain', '--outcome', 'failed', '--note', 'Checked the local execution receipt; no proposal completed.')
        self.assertEqual(code, 0)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(self.cli('resume', pid)[1]['paused'], False)

    def test_cli_generated_agent_id_survives_network_failure_without_starting_model(self):
        request = self.home / 'request.txt'
        request.write_text('A test request that must never reach Codex')
        with patch('unforge.Client.call', side_effect=ConnectionResetError('Simulated lost response')) as call:
            code, output, errors = self.cli('agent-start', 'a' * 32, '--from-file', str(request))
        self.assertEqual(code, 1)
        self.assertIsNone(output)
        events = [json.loads(line) for line in errors.splitlines()]
        operation = events[0]['operationId']
        self.assertEqual(events[-1]['operationId'], operation)
        self.assertEqual(call.call_args.args[1]['operationId'], operation)
        with patch('unforge.Client.call', return_value={'id': 'b' * 32, 'replayed': True}) as call:
            code, result, errors = self.cli('agent-start', 'a' * 32, '--from-file', str(request), '--operation-id', operation)
        self.assertEqual(code, 0)
        self.assertTrue(result['replayed'])
        self.assertEqual(errors, '')
        self.assertEqual(call.call_args.args[1]['operationId'], operation)

    def test_cli_lanes_merge_checks_and_observed_publish(self):
        project = self.client.call('/projects', {'name': 'Lane from CLI'})
        pid = project['id']
        live = self.home / 'observed-live'
        app = {'schemaVersion': 1, 'checks': {'schemaVersion': 1, 'jobs': [{'id': 'ok', 'command': ['python3', '-c', 'print("ok")']}]},
               'destinations': [{'id': 'live', 'type': 'local', 'path': str(live)}]}
        source = self.home / 'app.json'
        source.write_text(json.dumps(app))
        self.client.call(f'/projects/{pid}/file', {'path': '.unforge/app.json', 'content': source.read_text(), 'expectedContent': None})
        self.client.call(f'/projects/{pid}/save', {'message': 'Bind destination'})
        code, lane, _ = self.cli('lane-open', pid, '--name', 'Add note')
        self.assertEqual(code, 0)
        note = self.home / 'note.txt'
        note.write_text('from a lane\n')
        self.assertEqual(self.cli('lane-write', pid, lane['id'], 'note.txt', '--from-file', str(note))[0], 0)
        self.assertEqual(self.cli('lane-save', pid, lane['id'], 'Add a note')[0], 0)
        self.assertFalse((Engine(self.home / 'projects').root(pid) / 'note.txt').exists())
        code, merged, _ = self.cli('lane-merge', pid, lane['id'])
        self.assertEqual(code, 0)
        self.assertTrue(any(item['path'] == 'note.txt' for item in merged['files']) or
                        (Engine(self.home / 'projects').root(pid) / 'note.txt').read_text() == 'from a lane\n')
        self.assertEqual(self.cli('checks', pid)[1]['status'], 'passed')
        code, published, _ = self.cli('publish', pid, 'live')
        self.assertEqual(code, 0)
        self.assertTrue(published['observed'])
        self.assertTrue(self.cli('app', pid)[1]['githubAbsent'])


if __name__ == '__main__':
    unittest.main()
