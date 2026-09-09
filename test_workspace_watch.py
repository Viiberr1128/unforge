from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from workspace_watch import WorkspaceWatch


class WorkspaceWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.watchers = []

    def tearDown(self):
        for watcher in self.watchers: watcher.close()
        self.temp.cleanup()

    def watcher(self, script, callback=lambda: None):
        program = self.root / ('helper-' + str(len(self.watchers)) + '.py')
        program.write_text(script)
        watcher = WorkspaceWatch(self.root, callback, helper_command=[sys.executable, '-u', str(program)])
        self.watchers.append(watcher)
        return watcher

    def wait_for(self, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if predicate(): return
            time.sleep(0.01)
        self.fail('Watcher did not reach the expected state')

    def test_changed_event_calls_scheduler_without_paths(self):
        event = threading.Event()
        watcher = self.watcher("import sys\nprint('ready',flush=True)\nprint('changed',flush=True)\nsys.stdin.buffer.read()\n",event.set)
        self.assertTrue(event.wait(3))
        self.assertTrue(watcher.state()['observing'])
        self.assertEqual(watcher.state()['changesObserved'],1)
        self.assertNotIn(str(self.root),str(watcher.state()))
        watcher.close()
        self.assertIsNotNone(watcher.process.poll())
        self.assertFalse(watcher.reader.is_alive())

    def test_missing_helper_does_not_claim_observation(self):
        with patch('workspace_watch.helper_path',return_value=None):
            watcher = WorkspaceWatch(self.root,lambda:None)
        self.watchers.append(watcher)
        self.assertFalse(watcher.state()['available'])
        self.assertFalse(watcher.state()['observing'])
        self.assertEqual(watcher.state()['status'],'unavailable')

    def test_oversized_or_private_output_fails_closed(self):
        watcher = self.watcher("import sys\nprint('ready',flush=True)\nprint('private-path-'+'x'*200,flush=True)\nsys.stdin.buffer.read()\n")
        self.wait_for(lambda: watcher.state()['status'] == 'failed')
        self.assertNotIn('private-path',str(watcher.state()))
        self.assertFalse(watcher.state()['observing'])

    def test_helper_exit_is_visible(self):
        watcher = self.watcher("print('ready',flush=True)\n")
        self.wait_for(lambda: watcher.state()['status'] == 'failed')
        self.assertFalse(watcher.state()['observing'])

    def test_callback_failure_stops_observation(self):
        def fail(): raise RuntimeError('do not expose private paths')
        watcher = self.watcher("import sys\nprint('ready',flush=True)\nprint('changed',flush=True)\nsys.stdin.buffer.read()\n",fail)
        self.wait_for(lambda: watcher.state()['status'] == 'failed')
        self.assertNotIn('private paths',str(watcher.state()))
