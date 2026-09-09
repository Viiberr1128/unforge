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


if __name__ == '__main__':
    unittest.main()
