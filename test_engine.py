import http.client
import json
import socket
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from engine import Engine, Problem, Server

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(Path(self.temp.name) / 'projects')
        self.project = self.engine.create('Garden', 'A small personal project')
        self.pid = self.project['id']

    def tearDown(self):
        self.temp.cleanup()

    def test_versions_restore_preserves_history_and_bundle(self):
        original = self.project['history'][0]['id']
        self.engine.edit(self.pid, 'README.md', 'Updated\n')
        self.engine.edit(self.pid, 'notes.txt', 'Later file\n')
        saved = self.engine.save(self.pid, 'Update notes')
        updated = saved['history'][0]['id']
        restored = self.engine.restore(self.pid, original)
        self.assertEqual(len(restored['history']), 3)
        self.assertIn(updated, [h['id'] for h in restored['history']])
        self.assertNotIn('notes.txt', [f['path'] for f in restored['files']])
        bundle = Path(self.temp.name) / 'project.bundle'
        bundle.write_bytes(self.engine.export(self.pid))
        clone = Path(self.temp.name) / 'clone'
        self.engine.git(Path(self.temp.name), 'clone', str(bundle), str(clone))
        self.assertEqual((clone / 'README.md').read_text(), '# Garden\n\nA small personal project\n')
        self.assertEqual(len(self.engine.history(clone)), 3)
        self.assertEqual(json.loads((clone / '.unforge/project.json').read_text())['name'], 'Garden')

    def test_dirty_restore_refused(self):
        self.engine.edit(self.pid, 'README.md', 'Unsaved')
        with self.assertRaisesRegex(Problem, 'Save your current'):
            self.engine.restore(self.pid, self.project['history'][0]['id'])
        self.assertEqual((self.engine.root(self.pid) / 'README.md').read_text(), 'Unsaved')

    def test_paths_and_symlinks_rejected(self):
        for path in ('../outside', '/outside', '.git/config', '.env.local', 'secrets.json', 'credentials.json', 'a/../../outside', 'key.pem'):
            with self.subTest(path=path), self.assertRaises(Problem):
                self.engine.edit(self.pid, path, 'bad')
        root = self.engine.root(self.pid)
        (root / 'link').symlink_to(self.temp.name)
        with self.assertRaises(Problem):
            self.engine.edit(self.pid, 'link/outside', 'bad')
        with self.assertRaises(Problem):
            self.engine.save(self.pid, 'Do not snapshot symlinks')

    def test_import_refuses_case_unicode_and_file_directory_aliases_before_checkout(self):
        root = self.engine.root(self.pid)
        blob = self.engine.git(root, 'rev-parse', 'HEAD:README.md').decode().strip()
        directory = self.engine.git(root, 'rev-parse', 'HEAD:.unforge').decode().strip()
        original = self.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
        cases = [
            [('100644', 'Foo', blob), ('100644', 'foo', blob)],
            [('100644', 'caf\u00e9', blob), ('100644', 'cafe\u0301', blob)],
            [('100644', 'Cache', blob), ('40000', 'cache', directory)],
            [('40000', 'First', directory), ('40000', 'first', directory)],
        ]
        for entries in cases:
            with self.subTest(entries=[entry[1] for entry in entries]):
                # Create the Git tree directly: the host filesystem itself may
                # not support writing both aliases, which is the bug under test.
                records = [('40000', '.unforge', directory), *entries]
                records.sort(key=lambda value: (value[1] + ('/' if value[0] == '40000' else '')).encode())
                raw = b''.join(mode.encode() + b' ' + name.encode() + b'\0' + bytes.fromhex(oid)
                               for mode, name, oid in records)
                tree_file = Path(self.temp.name) / 'tree-object'
                tree_file.write_bytes(raw)
                tree = self.engine.git(root, 'hash-object', '-w', '-t', 'tree', str(tree_file)).decode().strip()
                commit = self.engine.git(root, 'commit-tree', tree, '-p', original, '-m', 'Cross-platform aliases').decode().strip()
                self.engine.git(root, 'update-ref', 'refs/heads/main', commit)
                bundle = Path(self.temp.name) / 'aliases.bundle'
                bundle.write_bytes(self.engine.export(self.pid))
                with self.assertRaisesRegex(Problem, 'collide by case or Unicode'):
                    self.engine.import_bundle(str(bundle))
                self.assertEqual(len(self.engine.projects()), 1)
                self.assertEqual((root / 'README.md').read_text(), '# Garden\n\nA small personal project\n')

    def test_validate_tree_allows_shared_directory_prefixes(self):
        self.engine.edit(self.pid, 'src/first.txt', 'First')
        self.engine.edit(self.pid, 'src/second.txt', 'Second')
        self.engine.save(self.pid, 'Add files in one directory')
        self.assertEqual(self.engine.validate_tree(self.engine.root(self.pid), 'HEAD')['name'], 'Garden')

    def test_request_is_explicit_handoff(self):
        detail = self.engine.request(self.pid, 'Add a checklist')
        request = next(f for f in detail['files'] if f['path'].startswith('.unforge/requests/'))
        self.assertIn('It has not been executed.', request['content'])
        self.assertTrue(detail['dirty'])

    def test_export_excludes_unsaved_changes(self):
        self.engine.edit(self.pid, 'uncommitted.txt', 'Do not export')
        bundle = Path(self.temp.name) / 'project.bundle'
        bundle.write_bytes(self.engine.export(self.pid))
        clone = Path(self.temp.name) / 'clone'
        self.engine.git(Path(self.temp.name), 'clone', str(bundle), str(clone))
        self.assertFalse((clone / 'uncommitted.txt').exists())

    def test_external_edit_collision_and_metadata_protection(self):
        root = self.engine.root(self.pid)
        old = (root / 'README.md').read_text(encoding='utf-8')
        (root / 'README.md').write_text('External change', encoding='utf-8')
        with self.assertRaisesRegex(Problem, 'changed outside'):
            self.engine.edit(self.pid, 'README.md', 'Overwrite', expected_content=old)
        self.assertEqual((root / 'README.md').read_text(encoding='utf-8'), 'External change')
        for path in ('.unforge/project.json', 'nested/.unforge/project.json'):
            with self.assertRaises(Problem):
                self.engine.edit(self.pid, path, '{}')
        detail = self.engine.detail(self.pid)
        self.assertTrue(next(f for f in detail['files'] if f['path'] == '.unforge/project.json')['readonly'])
        self.assertIn('updatedAt', self.engine.projects()[0])

    def test_restore_refuses_ignored_file_collision(self):
        root = self.engine.root(self.pid)
        self.engine.edit(self.pid, 'archive.txt', 'Original tracked file')
        old = self.engine.save(self.pid, 'Add archive')['history'][0]['id']
        (root / 'archive.txt').unlink()
        self.engine.edit(self.pid, '.gitignore', 'archive.txt\n')
        self.engine.save(self.pid, 'Remove archive and ignore it')
        (root / 'archive.txt').write_text('Important ignored content', encoding='utf-8')
        with self.assertRaisesRegex(Problem, 'ignored file'):
            self.engine.restore(self.pid, old)
        self.assertEqual((root / 'archive.txt').read_text(encoding='utf-8'), 'Important ignored content')

    def test_restore_refuses_case_variant_ignored_collision(self):
        root = self.engine.root(self.pid)
        self.engine.edit(self.pid, 'report.txt', 'Original tracked report')
        old = self.engine.save(self.pid, 'Add report')['history'][0]['id']
        (root / 'report.txt').unlink()
        self.engine.edit(self.pid, '.gitignore', '*.txt\n')
        self.engine.save(self.pid, 'Remove report')
        notes = root / 'REPORT.txt'
        notes.write_text('Important ignored notes', encoding='utf-8')
        with self.assertRaisesRegex(Problem, 'ignored file'):
            self.engine.restore(self.pid, old)
        self.assertEqual(notes.read_text(encoding='utf-8'), 'Important ignored notes')

    def test_restore_refuses_unicode_normalization_ignored_collision(self):
        root = self.engine.root(self.pid)
        self.engine.edit(self.pid, 'caf\u00e9.txt', 'Original tracked notes')
        old = self.engine.save(self.pid, 'Add notes')['history'][0]['id']
        (root / 'caf\u00e9.txt').unlink()
        self.engine.edit(self.pid, '.gitignore', '*.txt\n')
        self.engine.save(self.pid, 'Remove notes')
        notes = root / 'cafe\u0301.txt'
        notes.write_text('Important ignored notes', encoding='utf-8')
        with self.assertRaisesRegex(Problem, 'ignored file'):
            self.engine.restore(self.pid, old)
        self.assertEqual(notes.read_text(encoding='utf-8'), 'Important ignored notes')

    def test_restore_refuses_historical_symlink(self):
        root = self.engine.root(self.pid)
        (root / 'bad-link').symlink_to('/tmp')
        self.engine.git(root, 'add', 'bad-link')
        self.engine.git(root, 'commit', '-m', 'External unsafe version')
        unsafe = self.engine.history(root)[0]['id']
        self.engine.git(root, 'rm', 'bad-link')
        self.engine.git(root, 'commit', '-m', 'Remove unsafe link')
        with self.assertRaisesRegex(Problem, 'symbolic links'):
            self.engine.restore(self.pid, unsafe)
        self.assertFalse((root / 'bad-link').is_symlink())

    def _assert_imported_history_restore_bounded(self, file_count, blob_bytes):
        root = self.engine.root(self.pid)
        original = self.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
        current_tree = self.engine.git(root, 'rev-parse', 'HEAD^{tree}').decode().strip()
        metadata_tree = self.engine.git(root, 'rev-parse', 'HEAD:.unforge').decode().strip()
        # Repeated object references encode a large historical checkout while
        # the test writes only one small blob and a tree, never the large files.
        blob_file = Path(self.temp.name) / 'history-blob'
        blob_file.write_bytes(b'x' * blob_bytes)
        blob = self.engine.git(root, 'hash-object', '-w', str(blob_file)).decode().strip()
        entries = [('40000', '.unforge', metadata_tree)] + [
            ('100644', f'historical-{index:05d}.txt', blob) for index in range(file_count)]
        tree_file = Path(self.temp.name) / 'history-tree'
        tree_file.write_bytes(b''.join(mode.encode() + b' ' + name.encode() + b'\0' + bytes.fromhex(oid)
                                       for mode, name, oid in entries))
        tree = self.engine.git(root, 'hash-object', '-w', '-t', 'tree', str(tree_file)).decode().strip()
        oversized = self.engine.git(root, 'commit-tree', tree, '-p', original, '-m', 'Large historical version').decode().strip()
        current = self.engine.git(root, 'commit-tree', current_tree, '-p', oversized, '-m', 'Small current version').decode().strip()
        self.engine.git(root, 'update-ref', 'refs/heads/main', current)
        bundle = Path(self.temp.name) / 'historical.bundle'
        bundle.write_bytes(self.engine.export(self.pid))
        self.assertLess(bundle.stat().st_size, 1024 * 1024)
        imported = self.engine.import_bundle(str(bundle))
        imported_root = self.engine.root(imported['id'])
        git = self.engine.git

        def snapshot():
            return (git(imported_root, 'rev-parse', 'HEAD'),
                    (imported_root / '.git/index').read_bytes(),
                    {file.relative_to(imported_root).as_posix(): file.read_bytes()
                     for file in imported_root.rglob('*')
                     if '.git' not in file.relative_to(imported_root).parts and file.is_file()})

        before = snapshot()

        def bounded_git(directory, *args, **kwargs):
            # Keep a regressed implementation from expanding the hostile tree.
            if args and args[0] == 'restore':
                self.fail('Oversized historical checkout reached Git restore')
            return git(directory, *args, **kwargs)

        with patch.object(self.engine, 'git', side_effect=bounded_git), \
                patch.object(self.engine, 'validate_tree', wraps=self.engine.validate_tree) as validate_tree:
            with self.assertRaisesRegex(Problem, '128 MiB or 10,000 files'):
                self.engine.restore(imported['id'], oversized)
            validate_tree.assert_not_called()
        self.assertEqual(snapshot(), before)

    def test_restore_refuses_imported_oversized_historical_bytes(self):
        self._assert_imported_history_restore_bounded(129, 1024 * 1024)

    def test_restore_refuses_imported_oversized_historical_file_count(self):
        self._assert_imported_history_restore_bounded(10001, 1)

    def test_new_file_preconditions(self):
        self.engine.edit(self.pid, 'empty.txt', '', expected_content=None)
        with self.assertRaisesRegex(Problem, 'already exists'):
            self.engine.edit(self.pid, 'empty.txt', 'Overwrite', expected_content=None)
        with self.assertRaisesRegex(Problem, 'changed outside'):
            self.engine.edit(self.pid, 'missing.txt', 'Wrong precondition', expected_content='')
        self.engine.edit(self.pid, 'empty.txt', 'Existing now changed', expected_content='')

    def test_ignored_untracked_edits_refused_before_writing(self):
        root = self.engine.root(self.pid)
        self.engine.edit(self.pid, '.gitignore', '*.txt\n')
        self.engine.save(self.pid, 'Ignore generated text')
        with self.assertRaisesRegex(Problem, 'excluded from saved versions'):
            self.engine.edit(self.pid, 'notes.txt', 'Important notes', expected_content=None)
        self.assertFalse((root / 'notes.txt').exists())
        (root / 'existing.txt').write_text('Existing private text', encoding='utf-8')
        with self.assertRaisesRegex(Problem, 'excluded from saved versions'):
            self.engine.edit(self.pid, 'existing.txt', 'Overwrite', expected_content='Existing private text')
        self.assertEqual((root / 'existing.txt').read_text(encoding='utf-8'), 'Existing private text')

    def test_request_refuses_ignored_handoff_without_writing(self):
        self.engine.edit(self.pid, '.gitignore', '.unforge/requests/*\n')
        self.engine.save(self.pid, 'Ignore handoff records')
        with self.assertRaisesRegex(Problem, 'excluded from saved versions'):
            self.engine.request(self.pid, 'Important portable change request')
        self.assertFalse((self.engine.root(self.pid) / '.unforge/requests').exists())
        self.assertFalse(self.engine.detail(self.pid)['dirty'])

    def test_tracked_file_remains_editable_when_ignore_rule_matches(self):
        self.engine.edit(self.pid, '.gitignore', '*.md\n')
        self.engine.save(self.pid, 'Ignore new Markdown files')
        detail = self.engine.edit(self.pid, 'README.md', 'Tracked source stays editable')
        self.assertTrue(detail['dirty'])
        self.assertEqual(next(file['content'] for file in detail['files'] if file['path'] == 'README.md'), 'Tracked source stays editable')
        saved = self.engine.save(self.pid, 'Keep tracked source update')
        self.assertFalse(saved['dirty'])
        self.assertEqual(saved['history'][0]['message'], 'Keep tracked source update')

    def test_input_types_and_tabbed_history(self):
        with self.assertRaises(Problem):
            self.engine.root(None)
        with self.assertRaises(Problem):
            self.engine.restore(self.pid, {})
        self.engine.edit(self.pid, 'README.md', 'New')
        detail = self.engine.save(self.pid, 'Message\twith tab')
        self.assertEqual(detail['history'][0]['message'], 'Message\twith tab')
        self.assertRegex(detail['history'][0]['date'], r'^\d{4}-')

    def test_import_round_trip_preserves_refs_and_original(self):
        root = self.engine.root(self.pid)
        self.engine.edit(self.pid, 'notes.txt', 'Original notes')
        original = self.engine.save(self.pid, 'Add notes')
        self.engine.git(root, 'branch', 'another-version')
        self.engine.git(root, 'tag', 'v1')
        bundle = Path(self.temp.name) / 'portable.bundle'
        bundle.write_bytes(self.engine.export(self.pid))
        imported = self.engine.import_bundle(str(bundle))
        self.assertNotEqual(imported['id'], self.pid)
        self.assertEqual(imported['name'], 'Garden')
        self.assertEqual(imported['history'], original['history'])
        imported_root = self.engine.root(imported['id'])
        self.assertEqual(self.engine.git(root, 'show-ref'), self.engine.git(imported_root, 'show-ref'))
        self.engine.edit(imported['id'], 'notes.txt', 'Changed copy')
        self.assertEqual((root / 'notes.txt').read_text(encoding='utf-8'), 'Original notes')

    def test_import_rejects_bad_and_symlink_bundles(self):
        bundle = Path(self.temp.name) / 'bad.bundle'
        bundle.write_bytes(b'not a git bundle')
        with self.assertRaises(Problem):
            self.engine.import_bundle(str(bundle))
        root = self.engine.root(self.pid)
        (root / 'link').symlink_to('/tmp')
        self.engine.git(root, 'add', 'link')
        self.engine.git(root, 'commit', '-m', 'External symlink')
        bundle.write_bytes(self.engine.export(self.pid))
        with self.assertRaisesRegex(Problem, 'symbolic links'):
            self.engine.import_bundle(str(bundle))
        self.assertEqual(len(self.engine.projects()), 1)

    def test_server_home_lock_released_after_close(self):
        first = Server(('127.0.0.1', 0), self.engine, self.temp.name)
        try:
            with self.assertRaisesRegex(Problem, 'already using'):
                Server(('127.0.0.1', 0), self.engine, self.temp.name)
        finally:
            first.server_close()
        second = Server(('127.0.0.1', 0), self.engine, self.temp.name)
        second.server_close()

    def test_server_home_lock_released_after_bind_error(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            with self.assertRaises(OSError):
                Server(occupied.getsockname(), self.engine, self.temp.name)
        server = Server(('127.0.0.1', 0), self.engine, self.temp.name)
        server.server_close()

    def test_checkout_expansion_limits(self):
        with patch.object(self.engine, 'git', return_value=b'100644 blob abc 134217729\tlarge.txt\0'):
            with self.assertRaisesRegex(Problem, '128 MiB'):
                self.engine.validate_checkout_size(self.engine.home, 'HEAD')
        listing = b'100644 blob abc 1\ttiny.txt\0' * 10001
        with patch.object(self.engine, 'git', return_value=listing):
            with self.assertRaisesRegex(Problem, '10,000 files'):
                self.engine.validate_checkout_size(self.engine.home, 'HEAD')

    def test_http_authorization(self):
        server = Server(('127.0.0.1', 0), self.engine, self.temp.name)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            host = f'127.0.0.1:{server.server_port}'
            def call(method, path, headers=None, body=None):
                client = http.client.HTTPConnection('127.0.0.1', server.server_port)
                client.request(method, path, body=body, headers=headers or {})
                response = client.getresponse()
                status, data = response.status, response.read()
                client.close()
                return status, data
            status, health = call('GET', '/api/health')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(health), {'ok': True, 'version': '0.3.1'})
            status, data = call('GET', '/api/session')
            self.assertEqual(status, 200)
            token = json.loads(data)['token']
            headers = {'Content-Type':'application/json', 'Origin':'https://evil.example', 'X-Unforge-Token':token}
            self.assertEqual(call('POST', '/api/projects', headers, '{"name":"No"}')[0], 403)
            headers['Origin'] = 'http://' + host
            headers['X-Unforge-Token'] = 'wrong'
            self.assertEqual(call('POST', '/api/projects', headers, '{"name":"No"}')[0], 403)
            self.assertEqual(call('GET', '/api/session', {'Host':'evil.example'})[0], 403)
            headers['X-Unforge-Token'] = token
            self.assertEqual(call('POST', '/api/projects', headers, '{"name":"Yes"}')[0], 201)
            upload_headers = dict(headers, **{'Content-Type': 'application/octet-stream'})
            status, data = call('POST', '/api/import-bundle', upload_headers, self.engine.export(self.pid))
            self.assertEqual(status, 201)
            self.assertEqual(json.loads(data)['name'], 'Garden')
            self.assertEqual(call('POST', '/api/import-bundle', upload_headers, b'not a bundle')[0], 400)
            for endpoint, payload in (
                ('/api/projects', '[]'),
                ('/api/projects', '{"name":null}'),
                ('/api/projects', '{'),
                (f'/api/projects/{self.pid}/restore', '{"revision":{}}'),
                (f'/api/projects/{self.pid}/save', '{"message":null}'),
                (f'/api/projects/{self.pid}/file', '{"path":"README.md","content":"Oops","expectedContent":null}'),
                ('/api/projects', '[' * 1500 + ']' * 1500),
            ):
                with self.subTest(payload=payload[:80]):
                    status, data = call('POST', endpoint, headers, payload)
                    self.assertEqual(status, 400)
                    self.assertIn('error', json.loads(data))
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_http_reads_reject_foreign_browser_context_and_preserve_local_clients(self):
        from unforge import Client
        (Path(self.temp.name) / 'index.html').write_text('<!doctype html><title>Local app</title>')
        server = Server(('127.0.0.1', 0), self.engine, self.temp.name)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            origin = f'http://127.0.0.1:{server.server_port}'

            def read(path, headers):
                client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                try:
                    client.request('GET', path, headers=headers)
                    response = client.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    client.close()

            for headers in (
                {'Origin': 'https://evil.example'},
                {'Origin': 'null'},
                {'Origin': 'http://127.0.0.1:9999'},
                {'Sec-Fetch-Site': 'cross-site'},
                {'Sec-Fetch-Site': 'same-site'},
                {'Origin': origin, 'Sec-Fetch-Site': 'cross-site'},
                {'Origin': 'https://evil.example', 'Sec-Fetch-Site': 'same-origin'},
            ):
                for path in ('/api/session', '/api/projects', f'/api/projects/{self.pid}/export', '/'):
                    with self.subTest(headers=headers, path=path):
                        status, _, body = read(path, headers)
                        self.assertEqual(status, 403)
                        self.assertNotIn(server.token.encode(), body)
                        self.assertNotIn(b'Garden', body)

            # CLI and native URLSession omit browser metadata. WKWebView direct
            # navigation uses none/absent; application fetches use same-origin.
            self.assertTrue(Client(server.server_port).call('/health')['ok'])
            for headers in ({}, {'Sec-Fetch-Site': 'none'}, {'Sec-Fetch-Site': 'same-origin'},
                            {'Origin': origin, 'Sec-Fetch-Site': 'same-origin'}):
                with self.subTest(local_headers=headers):
                    status, _, body = read('/api/session', headers)
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body)['token'], server.token)
            # The development proxy rewrites its trusted Origin to the backend
            # origin and retains the browser's same-origin fetch metadata.
            status, _, body = read('/api/projects', {'Origin': origin, 'Sec-Fetch-Site': 'same-origin'})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['projects'][0]['id'], self.pid)
            localhost = f'localhost:{server.server_port}'
            self.assertEqual(read('/api/session', {'Host': localhost, 'Origin': 'http://' + localhost,
                                                   'Sec-Fetch-Site': 'same-origin'})[0], 200)
            for path in ('/', '/api/session'):
                status, headers, _ = read(path, {})
                self.assertEqual(status, 200)
                self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
                policy = headers['Content-Security-Policy']
                for directive in ("base-uri 'none'", "form-action 'none'", "object-src 'none'"):
                    self.assertIn(directive, policy)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

if __name__ == '__main__':
    unittest.main()
