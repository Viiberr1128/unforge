"""Owned native file-event subscription; no polling or private paths in messages."""
import os
from pathlib import Path
import signal
import selectors
import time
import subprocess
import sys
import threading


def helper_path():
    candidates = [Path(__file__).parent / '.tools/unforge-workspace-watch',
                  Path(sys.executable).parent / 'unforge-workspace-watch',
                  Path(sys.executable).parent.parent / 'unforge-workspace-watch',
                  Path(__file__).parent / 'unforge-workspace-watch']
    return next((str(path) for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


class WorkspaceWatch:
    def __init__(self, home, on_changed, helper_command=None):
        self.home = Path(home).resolve()
        self.on_changed = on_changed
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._ready = threading.Event()
        self.process = None
        self.reader = None
        self._status = 'unavailable'
        self._error = None
        self._changes = 0
        helper = helper_path() if sys.platform == 'darwin' else None
        command = helper_command or ([helper] if helper else None)
        if command is None:
            return
        if not self.home.is_dir():
            self._status = 'failed'
            self._error = 'Workspace folder is unavailable.'
            return
        try:
            self.process = subprocess.Popen([*command, str(self.home)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={key:value for key,value in os.environ.items() if key in {'PATH','SYSTEMROOT','LANG'}},
                cwd=self.home, start_new_session=True)
            self._status = 'starting'
            self.reader = threading.Thread(target=self._read, name='unforge-workspace-events', daemon=True)
            self.reader.start()
        except OSError:
            self._status = 'failed'
            self._error = 'Native workspace watcher could not start.'

    def _read(self):
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        buffer = b''
        deadline = time.monotonic() + 5
        try:
            while not self._closed.is_set():
                if not self._ready.is_set() and time.monotonic() > deadline:
                    raise ValueError('Watcher did not become ready')
                events = selector.select(timeout=0.5)
                if not events:
                    if self.process.poll() is not None: break
                    continue
                chunk = os.read(self.process.stdout.fileno(), 64)
                if not chunk: break
                buffer += chunk
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    if line == b'ready' and not self._ready.is_set():
                        with self._lock: self._status = 'watching'
                        self._ready.set()
                    elif line == b'changed' and self._ready.is_set():
                        self.on_changed()
                        with self._lock: self._changes += 1
                    else:
                        raise ValueError('Invalid watcher protocol')
                if len(buffer) > 16: raise ValueError('Invalid watcher protocol')
            with self._lock:
                if not self._closed.is_set():
                    self._status = 'failed'
                    self._error = 'Native workspace watcher stopped; external edits are not being observed.'
        except Exception:
            with self._lock:
                if not self._closed.is_set():
                    self._status = 'failed'
                    self._error = 'Native workspace watcher failed; external edits are not being observed.'
            self._terminate(signal.SIGTERM)
        finally:
            selector.close()
            if self.process:
                try: self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._terminate(signal.SIGKILL)
                    self.process.wait(timeout=2)

    def state(self):
        with self._lock:
            return dict(available=self.process is not None, status=self._status,
                        observing=self._status == 'watching', changesObserved=self._changes,
                        error=self._error, mechanism='macOS FSEvents' if self.process else None,
                        scope='Managed workspace subtree. Operational backup/runtime state is excluded. Events trigger scheduling; they do not prove backup or cloud upload.')

    def _terminate(self, sig):
        if self.process is not None and self.process.poll() is None:
            try: os.killpg(self.process.pid, sig)
            except ProcessLookupError: pass

    def close(self):
        self._closed.set()
        if self.process and self.process.stdin:
            try: self.process.stdin.close()
            except OSError: pass
        if self.reader:
            self.reader.join(timeout=2)
        if self.reader and self.reader.is_alive():
            self._terminate(signal.SIGTERM)
            self.reader.join(timeout=2)
        if self.reader and self.reader.is_alive():
            self._terminate(signal.SIGKILL)
            self.reader.join(timeout=2)
        if self.process:
            try: self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._terminate(signal.SIGKILL)
                self.process.wait(timeout=2)
            if self.process.stdout: self.process.stdout.close()
        with self._lock: self._status = 'closed'
