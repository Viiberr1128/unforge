"""Desktop engine ownership tests use only disposable workspaces and no model calls."""
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from engine import Engine, Server
from unforge import Client

class DesktopLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'workspace'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.process = subprocess.Popen([sys.executable, str(Path(__file__).parent / 'launcher.py'),
            '--port', str(self.port), '--home', str(self.home), '--desktop-parent', '--desktop-id', 'test-parent'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.addCleanup(self.cleanup_process)
        self.client = Client(self.port)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                self.client.call('/health')
                break
            except (OSError, ValueError):
                if self.process.poll() is not None:
                    self.fail(self.process.communicate()[1].decode())
                time.sleep(.03)
        else:
            self.fail('Desktop engine did not become ready')

    def cleanup_process(self):
        if self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            if pipe and not pipe.closed: pipe.close()

    def test_parent_pipe_closure_preserves_project_and_releases_workspace(self):
        project = self.client.call('/projects', {'name': 'Desktop persistence'})
        self.process.stdin.close()
        self.assertEqual(self.process.wait(timeout=8), 0)
        server = Server(('127.0.0.1', self.port), Engine(self.home), self.temp.name)
        try:
            self.assertEqual(server.engine.detail(project['id'])['name'], 'Desktop persistence')
        finally:
            server.server_close()

    def test_desktop_identity_is_exact_and_still_requires_local_host(self):
        info = self.client.call('/desktop')
        self.assertEqual(info['workspace'], str(self.home.resolve()))
        self.assertEqual(info['desktopId'], 'test-parent')
        self.assertEqual(info['pid'], self.process.pid)
        self.assertTrue(info['managed'])
        self.assertEqual(info['pendingWork'], 0)
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        try:
            conn.request('GET', '/api/desktop', headers={'Host': 'outside.example'})
            self.assertEqual(conn.getresponse().status, 403)
        finally: conn.close()

    def test_termination_releases_engine_without_deleting_saved_files(self):
        project = self.client.call('/projects', {'name': 'Quit safely'})
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=8), 0)
        self.assertTrue((self.home / project['id'] / 'README.md').is_file())
        server = Server(('127.0.0.1', self.port), Engine(self.home), self.temp.name)
        server.server_close()
