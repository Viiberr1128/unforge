"""Operating reads and new API workflows use disposable ordinary Git only."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from change_feed import ChangeFeed
from engine import Engine, Server
from operating import Operating
from unforge import Client

class OperatingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name) / 'workspace')
        self.pid = self.engine.create('Operating fixture')['id']
        self.root = self.engine.root(self.pid)

    def test_working_and_production_refs_are_separate_without_mutation(self):
        old = self.engine.git(self.root, 'rev-parse', 'HEAD').decode().strip()
        self.engine.git(self.root, 'update-ref', 'refs/example/production', old)
        self.engine.edit(self.pid, 'next.txt', 'Next')
        self.engine.save(self.pid, 'Next source')
        new = self.engine.git(self.root, 'rev-parse', 'HEAD').decode().strip()
        result = Operating(self.engine).get(self.pid)
        self.assertEqual(result['sourceVersion'], new)
        self.assertEqual(result['productionVersion'], old)
        self.assertEqual(result['divergence'], {'workingOnly': 1, 'productionOnly': 0})
        self.assertEqual(len(result['worktrees']), 1)
        self.assertEqual(self.engine.git(self.root, 'rev-parse', 'HEAD').decode().strip(), new)
        self.assertFalse(self.engine.git(self.root, 'status', '--porcelain'))

    def test_multiple_production_candidates_require_a_choice(self):
        head = self.engine.git(self.root, 'rev-parse', 'HEAD').decode().strip()
        for name in ('one', 'two'):
            self.engine.git(self.root, 'update-ref', f'refs/{name}/production', head)
        result = Operating(self.engine).get(self.pid)
        self.assertIsNone(result['productionVersion'])
        self.assertEqual(len(result['productionRefCandidates']), 2)

    def test_invalid_ref_setting_is_reported_without_mutation(self):
        self.engine.edit(self.pid, '.unforge/app.json', json.dumps({'productionRef': '--help'}))
        result = Operating(self.engine).get(self.pid)
        self.assertIsNotNone(result['productionError'])
        self.assertIsNone(result['productionVersion'])

    def test_feed_publish_and_close_wake_waiters(self):
        feed = ChangeFeed()
        feed.publish()
        self.assertEqual(feed.wait(0, timeout=0), (1, False))
        feed.close()
        self.assertEqual(feed.wait(1, timeout=0), (1, True))

    def test_malformed_packaged_build_identity_is_reported(self):
        import build_info
        build_info.current.cache_clear()
        self.addCleanup(build_info.current.cache_clear)
        with mock.patch.object(build_info.sys, 'frozen', True, create=True), mock.patch.object(Path, 'open', mock.mock_open(read_data=b'[]')):
            self.assertEqual(build_info.current()['version'], 'unknown')
            self.assertIn('metadataError', build_info.current())

class OperatingApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = Server(('127.0.0.1', 0), Engine(Path(self.temp.name) / 'workspace'), self.temp.name)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = Client(self.server.server_port)

    def test_build_identity_and_immutable_production_lane_route(self):
        build = self.client.call('/build')
        self.assertEqual(build['version'], json.loads((Path(__file__).parent/'package.json').read_text())['version'])
        self.assertIn('operating-view', build['capabilities'])
        project = self.client.call('/projects', {'name':'API fixture'})
        pid = project['id']; root = self.server.engine.root(pid)
        old = self.server.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
        self.server.engine.git(root, 'update-ref', 'refs/example/production', old)
        self.server.engine.edit(pid, 'unrelated-draft.txt', 'Keep this draft')
        dirty_before = self.server.engine.git(root, 'status', '--porcelain')
        lane = self.client.call(f'/projects/{pid}/lanes', {'name':'Production candidate','baseRef':'refs/example/production'})
        self.assertEqual(lane['base'], old)
        self.assertEqual(self.server.engine.git(root, 'status', '--porcelain'), dirty_before)
        self.assertEqual((root/'unrelated-draft.txt').read_text(), 'Keep this draft')
        content = self.client.call(f'/projects/{pid}/lanes/{lane["id"]}/content?path=README.md')
        with self.assertRaises(ValueError):
            self.client.call(f'/projects/{pid}/lanes/{lane["id"]}/file', {'path':'README.md','content':'replacement','expectedContent':'outdated'})
        self.assertEqual(self.client.call(f'/projects/{pid}/lanes/{lane["id"]}/content?path=README.md')['content'], content['content'])

    def test_event_stream_announces_successful_source_mutation_and_closes(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(conn.close)
        conn.request('GET','/api/events')
        stream = conn.getresponse()
        self.assertEqual(stream.status, 200)
        self.assertEqual(stream.getheader('Content-Type'), 'text/event-stream')
        first = json.loads(stream.readline().decode()[6:])['revision']
        stream.readline()
        self.client.call('/projects', {'name':'Event source'})
        second = json.loads(stream.readline().decode()[6:])['revision']
        self.assertGreater(second, first)
        stream.close()

    def test_cross_origin_event_stream_is_rejected(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(conn.close)
        conn.request('GET','/api/events',headers={'Origin':'https://elsewhere.invalid'})
        response = conn.getresponse()
        self.assertEqual(response.status, 403)
        response.read()

    def test_event_stream_limit_keeps_other_api_requests_available(self):
        for _ in range(8):
            self.assertTrue(self.server.event_slots.acquire(blocking=False))
            self.addCleanup(self.server.event_slots.release)
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(conn.close)
        conn.request('GET', '/api/events')
        response = conn.getresponse()
        self.assertEqual(response.status, 503)
        response.read()
        self.assertTrue(self.client.call('/health')['ok'])

    def test_observation_route_preserves_explicit_production_revision(self):
        project = self.client.call('/projects', {'name':'Observation fixture'})
        revision = self.server.engine.git(self.server.engine.root(project['id']), 'rev-parse', 'HEAD').decode().strip()
        with mock.patch.object(self.server.releases, 'observe', return_value={'tree':revision}) as observe:
            self.client.call(f'/projects/{project["id"]}/releases/observe', {'destinationId':'existing-provider','tree':revision})
            observe.assert_called_once_with(project['id'], 'existing-provider', tree=revision)
