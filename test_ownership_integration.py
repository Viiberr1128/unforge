"""Ownership journeys through the real loopback API, using disposable workspaces."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import quote, urlsplit

from backups import restic_path
from engine import Engine, Server


class OwnershipIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='unforge-http-ownership-')
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / 'workspace'
        self.server = None
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.close_server)
        self.open_server()

    def open_server(self):
        self.server = Server(('127.0.0.1', 0), Engine(self.home), self.base / 'static')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)
        self.token = self.call('/session')['token']

    def close_server(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(5)
            self.assertFalse(self.thread.is_alive(), 'HTTP server did not stop')
            self.server = None

    def call(self, path, data=None, *, status=200, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=40)
        request_headers = {}
        body = None
        if data is not None:
            request_headers = {'Content-Type': 'application/json', 'Origin': self.origin,
                               'X-Unforge-Token': self.token}
            body = json.dumps(data)
        request_headers.update(headers or {})
        try:
            connection.request('POST' if data is not None else 'GET', '/api' + path,
                               body=body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            self.assertEqual(response.status, status, (path, raw.decode(errors='replace')))
            return json.loads(raw)
        finally:
            connection.close()

    def create(self, name='Owned fixture'):
        return self.call('/projects', {'name': name}, status=201)

    def wait_for(self, read, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = read()
            if predicate(value):
                return value
            time.sleep(.05)
        self.fail(f'Operation did not reach the expected state: {value}')

    def test_folder_review_independent_import_and_complete_file_navigation(self):
        source = self.base / 'original'; source.mkdir()
        for index in range(127):
            (source / f'note-{index:03}.txt').write_text(f'Original note {index}')
        (source / '.env').write_text('PRIVATE=fixture-only')
        (source / 'data').mkdir()
        (source / 'data/keep.txt').write_text('Original app data')
        inventory = self.call('/folders/inventory', {'path': str(source)})
        self.assertTrue(inventory['partial'])
        self.assertEqual({item['path'] for item in inventory['skipped']}, {'.env', 'data/'})
        request = {'path': str(source), 'revision': inventory['revision'], 'name': 'My imported app'}
        self.call('/folders/import', request, status=400)
        self.assertEqual(self.call('/projects')['projects'], [])
        adopted = self.call('/folders/import', {**request, 'allowPartial': True}, status=201)
        prefix = '/projects/' + adopted['project']['id']
        self.assertTrue(adopted['adoption']['partial'])
        self.assertEqual(self.call(prefix + '/adoption')['sourcePath'], str(source))
        names, cursor = [], 0
        while cursor is not None:
            page = self.call(prefix + f'/files?cursor={cursor}&limit=31')
            names.extend(item['path'] for item in page['files'])
            cursor = page['nextCursor']
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue({f'note-{i:03}.txt' for i in range(127)}.issubset(names))
        self.assertNotIn('.env', names)
        last = self.call(prefix + '/content?path=note-126.txt')
        self.assertEqual(last['content'], 'Original note 126')
        self.call(prefix + '/file', {'path': 'note-126.txt', 'content': 'Owned edit',
                                    'expectedContent': last['content']})
        self.assertEqual((source / 'note-126.txt').read_text(), 'Original note 126')
        self.assertEqual((source / 'data/keep.txt').read_text(), 'Original app data')
        self.assertFalse((source / '.git').exists())
        self.call(prefix + '/content?path=' + quote('../outside'), status=400)

    def test_draft_restart_conflict_and_explicit_save_preserve_work(self):
        project = self.create()
        prefix = '/projects/' + project['id']
        original = self.call(prefix + '/content?path=README.md')['content']
        draft = self.call(prefix + '/draft', {'path': 'README.md', 'content': 'My unsaved thinking',
                                            'baseContent': original, 'revision': None})
        self.assertNotIn('content', self.call(prefix + '/drafts')['drafts'][0])
        old_token = self.token
        self.close_server(); self.open_server()
        self.call(prefix + '/draft/discard', {'path': 'README.md', 'revision': draft['revision']},
                  status=403, headers={'X-Unforge-Token': old_token})
        recovered = self.call(prefix + '/draft?path=README.md')
        self.assertEqual(recovered['content'], 'My unsaved thinking')
        self.assertEqual(recovered['revision'], draft['revision'])
        self.assertEqual(self.call(prefix + '/content?path=README.md')['content'], original)
        self.call(prefix + '/file', {'path': 'README.md', 'content': 'A newer change',
                                    'expectedContent': original})
        self.assertTrue(self.call(prefix + '/draft?path=README.md')['stale'])
        self.call(prefix + '/file', {'path': 'README.md', 'content': recovered['content'],
                                    'expectedContent': recovered['baseContent']}, status=400)
        self.assertEqual(self.call(prefix + '/draft?path=README.md')['content'], recovered['content'])
        merged = self.call(prefix + '/draft', {'path': 'README.md', 'content': 'Both changes kept',
                                             'baseContent': 'A newer change', 'revision': recovered['revision']})
        self.call(prefix + '/draft/discard', {'path': 'README.md', 'revision': recovered['revision']}, status=400)
        self.call(prefix + '/file', {'path': 'README.md', 'content': merged['content'],
                                    'expectedContent': merged['baseContent']})
        self.call(prefix + '/draft/discard', {'path': 'README.md', 'revision': merged['revision']})
        self.assertIsNone(self.call(prefix + '/draft?path=README.md'))
        self.assertEqual(self.call(prefix + '/content?path=README.md')['content'], 'Both changes kept')

    def test_new_mutation_routes_require_loopback_origin_and_current_token(self):
        project = self.create(); prefix = '/projects/' + project['id']
        for route, document in [('/folders/inventory', {'path': str(self.base)}),
                                ('/folders/import', {'path': str(self.base)}),
                                ('/backups/settings', {}), ('/backups/start', {}),
                                ('/backups/restore', {}), ('/backups/automatic', {'enabled': True}),
                                (prefix + '/draft', {}), (prefix + '/runtime/configure', {}),
                                (prefix + '/runtime/start', {'trusted': True, 'persistent': True}),
                                (prefix + '/runtime/stop', {})]:
            with self.subTest(route=route):
                self.call(route, document, status=403, headers={'X-Unforge-Token': ''})
                self.call(route, document, status=403, headers={'Origin': 'https://untrusted.example'})
        self.call('/backups', status=403, headers={'Host': 'untrusted.example'})
        self.assertFalse(self.call('/backups')['configured'])
        self.assertEqual(self.call(prefix + '/runtime')['runs'], [])
        self.assertEqual(self.call(prefix + '/drafts')['drafts'], [])

    @unittest.skipUnless(restic_path(), 'Pinned local Restic is needed for encrypted backup integration')
    def test_unavailable_backup_destination_records_failure_without_false_protection(self):
        self.create()
        destination = self.base / 'unavailable-drive'
        self.call('/backups/settings', {'destinations': [{'kind': 'folder', 'path': str(destination)}],
                                       'password': 'fixture independent recovery passphrase'})
        destination.rmdir()
        destination.write_text('Another file occupies the disconnected destination')
        job = self.call('/backups/start', {}, status=202)
        failed = self.wait_for(lambda: next(j for j in self.call('/backups')['jobs'] if j['id'] == job['id']),
                               lambda j: j['state'] != 'running', timeout=35)
        self.assertEqual(failed['state'], 'failed', failed)
        self.assertTrue(failed['error'])
        self.assertFalse(any(d.get('state') == 'completed' for d in failed['destinations']))
        self.assertEqual(destination.read_text(), 'Another file occupies the disconnected destination')
        self.close_server(); self.open_server()
        recovered = next(j for j in self.call('/backups')['jobs'] if j['id'] == job['id'])
        self.assertEqual(recovered['state'], 'failed')

    @unittest.skipUnless(restic_path(), 'Pinned local Restic is needed for encrypted backup integration')
    def test_backup_api_restores_drafts_and_source_with_failure_receipts(self):
        project = self.create(); prefix = '/projects/' + project['id']
        self.call('/backups/start', {}, status=400)
        original = self.call(prefix + '/content?path=README.md')['content']
        self.call(prefix + '/draft', {'path': 'README.md', 'content': 'Recover my unsaved draft',
                                     'baseContent': original, 'revision': None})
        self.call(prefix + '/file', {'path': 'unsaved.txt', 'content': 'Written but not committed',
                                    'expectedContent': None})
        destination = self.base / 'independent-backups'
        password = 'independent test recovery passphrase'
        settings = self.call('/backups/settings', {'destinations': [{'kind': 'folder', 'path': str(destination)}],
                                                  'password': password})
        self.assertTrue(settings['configured'])
        self.assertNotIn(password, json.dumps(settings))
        job = self.call('/backups/start', {}, status=202)
        complete = self.wait_for(lambda: next(j for j in self.call('/backups')['jobs'] if j['id'] == job['id']),
                                 lambda j: j['state'] != 'running', timeout=35)
        self.assertEqual(complete['state'], 'completed', complete)
        archive = complete['destinations'][0]['backupPath']
        restore = {'path': archive, 'destination': str(self.base / 'recovered'), 'password': password}
        self.call('/backups/restore', {**restore, 'password': 'wrong recovery key'}, status=400)
        self.assertFalse((self.base / 'recovered').exists())
        result = self.call('/backups/restore', restore)
        self.assertFalse(result['applicationsStarted'])
        recovered_home = Path(result['path'])
        self.assertEqual((recovered_home / project['id'] / 'unsaved.txt').read_text(), 'Written but not committed')
        self.call('/backups/restore', restore, status=400)
        # Reopen the recovered workspace through HTTP, independently of the original key.
        self.close_server(); self.home = recovered_home; self.open_server()
        self.assertEqual(self.call(prefix + '/draft?path=README.md')['content'], 'Recover my unsaved draft')
        self.assertFalse(self.call('/backups')['configured'])
        self.assertEqual(self.call(prefix + '/content?path=unsaved.txt')['content'], 'Written but not committed')

    def test_runtime_api_config_revision_and_local_data_survive_restart(self):
        project = self.create(); prefix = '/projects/' + project['id']
        script = '''import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
counter = Path(os.environ['UNFORGE_DATA_DIR']) / 'visits.txt'
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        n = int(counter.read_text()) if counter.exists() else 0
        if self.path == '/increment':
            n += 1
            counter.write_text(str(n))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(str(n).encode())
HTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()
'''
        self.call(prefix + '/file', {'path': 'app.py', 'content': script, 'expectedContent': None})
        document = {'schemaVersion': 1, 'profile': 'python', 'run': ['python3', 'app.py']}
        configured = self.call(prefix + '/runtime/configure', {'document': document, 'revision': None})
        self.call(prefix + '/runtime/configure', {'document': document, 'revision': None}, status=400)
        self.call(prefix + '/runtime/start', {'trusted': True, 'persistent': True}, status=400)
        self.call(prefix + '/save', {'message': 'Save app and execution settings'})
        self.call(prefix + '/runtime/start', {'trusted': False, 'persistent': True}, status=400)
        data_paths = []
        for expected in ('1', '2'):
            attempt = self.call(prefix + '/runtime/start', {'trusted': True, 'persistent': True})
            ready = self.wait_for(lambda: next(r for r in self.call(prefix + '/runtime')['runs'] if r['id'] == attempt['id']),
                                  lambda r: r['health'] == 'responding')
            self.assertTrue(ready['persistent'])
            data_paths.append(ready['dataPath'])
            endpoint = urlsplit(ready['url'])
            connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=5)
            try:
                connection.request('GET', '/increment')
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read().decode(), expected)
            finally:
                connection.close()
            self.call(prefix + '/runtime/stop', {'runId': attempt['id']})
            self.wait_for(lambda: next(r for r in self.call(prefix + '/runtime')['runs'] if r['id'] == attempt['id']),
                          lambda r: r['status'] == 'stopped')
            self.close_server(); self.open_server()
            self.assertEqual(self.call(prefix + '/runtime')['revision'], configured['revision'])
        self.assertEqual(data_paths[0], data_paths[1])
        self.assertFalse((self.server.engine.root(project['id']) / 'visits.txt').exists())
        self.assertFalse(self.call('/runtime')['active'])


if __name__ == '__main__':
    unittest.main()
