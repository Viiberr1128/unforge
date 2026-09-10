"""Explicit trusted local execution in disposable candidates; this is not a sandbox."""
from datetime import datetime, timezone
import argparse
import copy
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unicodedata
from urllib.parse import unquote, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler
import uuid

from engine import Problem

MAX_LOG = 128 * 1024
MAX_CONFIG = 32 * 1024
MAX_ATTEMPTS = 100
ACTIVE = {'starting', 'preparing', 'running'}
TOOLS = {'static': (), 'node': ('npm', 'node'), 'python': ('python3', 'python'), 'swift': ('swift', 'xcrun')}
RESERVED_DATA_ENV = {'PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'PWD', 'OLDPWD', 'TMP', 'TMPDIR', 'TEMP',
                     'LANG', 'SYSTEMROOT', 'VIRTUAL_ENV', 'CONDA_PREFIX', 'NODE_ENV', 'CI', 'NO_COLOR',
                     'ENV', 'BASH_ENV', 'BASHOPTS', 'SHELLOPTS', 'IFS', 'CDPATH', 'ZDOTDIR', 'PROMPT_COMMAND',
                     'NODE_OPTIONS', 'NODE_PATH', 'PORT', 'HOST', 'UNFORGE_PORT', 'UNFORGE_HOME',
                     'RUBYOPT', 'PERL5OPT', 'SWIFT_EXEC', 'SDKROOT', 'DEVELOPER_DIR', 'CC', 'CXX', 'RUSTC_WRAPPER',
                     'UNFORGE_DATA_DIR', 'UNFORGE_LOCAL_TEST', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
                     'OPENSSL_CONF', 'OPENSSL_MODULES', 'MAKEFLAGS', 'CMAKE_BUILD_PARALLEL_LEVEL',
                     'SWIFTPM_MAXIMUM_CONCURRENT_OPERATIONS'}
LIMITS = 'Trusted local execution, not a sandbox. Project code can access this Mac and the network. Only run code you trust. Production credentials are not inherited; source may still contain credentials or external calls. Commands that deliberately detach into another process session are unsupported.'


def now():
    return datetime.now(timezone.utc).isoformat()


def data_relative(value, *, allow_root=False):
    if allow_root and value == '.':
        return value
    if not isinstance(value, str) or not value or len(value) > 240 or value.startswith('/') or '\\' in value or ':' in value or any(ord(c) < 32 for c in value):
        raise Problem('Data paths must stay relative to the project data directory')
    for part in value.split('/'):
        lower = part.lower()
        if part in ('', '.', '..') or lower in ('.git', '.unforge', '.gitconfig', '.gitmodules') or lower.startswith('.env'):
            raise Problem('Private, reserved, and parent paths cannot be used for runtime data')
    return value


def data_target(root, relative):
    """Resolve a declared data target without following existing symbolic links."""
    target = root
    if root.is_symlink():
        raise Problem('Runtime data storage must not be a symbolic link')
    for part in [] if relative == '.' else relative.split('/'):
        target /= part
        if target.is_symlink():
            raise Problem('Runtime data paths must not follow symbolic links')
    if not target.resolve().is_relative_to(root.resolve()):
        raise Problem('Runtime data path escapes its data directory')
    return target


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def validate(document):
    if not isinstance(document, dict):
        raise Problem('Runtime settings must be an object')
    allowed = {'schemaVersion', 'profile', 'directory', 'run', 'check', 'prepare', 'healthPath',
               'startupTimeoutSeconds', 'checkTimeoutSeconds', 'runTimeoutSeconds', 'dataPaths', 'dataEnvironment'}
    if set(document) - allowed or document.get('schemaVersion') != 1 or document.get('profile') not in TOOLS:
        raise Problem('Use runtime settings schemaVersion 1 and a static, node, python, or swift profile')
    result = copy.deepcopy(document)
    directory = result.setdefault('directory', '.')
    if not isinstance(directory, str) or len(directory) > 240 or directory.startswith('/') or '\\' in directory or any(ord(c) < 32 for c in directory):
        raise Problem('Choose a project-relative runtime directory')
    if directory != '.' and any(part in ('', '.', '..') or part.startswith('.') for part in directory.split('/')):
        raise Problem('Runtime directory must stay inside the project')
    for name in ('run', 'check', 'prepare'):
        command = result.setdefault(name, [])
        if not isinstance(command, list) or len(command) > 64 or any(not isinstance(arg, str) or not arg or len(arg) > 2000 or '\0' in arg or '\n' in arg for arg in command):
            raise Problem(f'{name} must be an array of command arguments')
        if command:
            if command[0] not in TOOLS[result['profile']]:
                raise Problem(f'{name} must use a tool from the selected profile')
            if command[0] == 'xcrun' and (len(command) < 2 or command[1] not in ('swift', 'swiftc', 'xcodebuild')):
                raise Problem('The Swift profile supports xcrun swift, swiftc, or xcodebuild')
        if result['profile'] == 'static' and command:
            raise Problem('Static projects use the built-in server and source checks')
    if result['profile'] != 'static' and not result['run']:
        raise Problem('Choose the explicit command that starts this app')
    paths = result.setdefault('dataPaths', [])
    if not isinstance(paths, list) or len(paths) > 16:
        raise Problem('Choose at most 16 data directories to mount')
    normalized = []
    for relative in paths:
        data_relative(relative)
        key = unicodedata.normalize('NFC', relative).casefold()
        if any(key == previous or key.startswith(previous + '/') or previous.startswith(key + '/') for previous in normalized):
            raise Problem('Runtime data directories must not overlap or alias one another')
        normalized.append(key)
    environment = result.setdefault('dataEnvironment', {})
    if not isinstance(environment, dict) or len(environment) > 24:
        raise Problem('Choose at most 24 data environment variables')
    for name, relative in environment.items():
        if (not isinstance(name, str) or not re.fullmatch('[A-Z][A-Z0-9_]{0,63}', name)
                or name in RESERVED_DATA_ENV or name.startswith(('LD_', 'DYLD_', 'PYTHON', 'GIT_', 'NPM_', 'XDG_', 'LC_'))
                or any(word in name for word in ('SECRET', 'TOKEN', 'PASSWORD', 'API_KEY', 'CREDENTIAL'))):
            raise Problem('Use an app-specific data variable; system, loader, and credential variables are reserved')
        data_relative(relative, allow_root=True)
    for name, default, maximum in [('startupTimeoutSeconds', 30, 120), ('checkTimeoutSeconds', 300, 900), ('runTimeoutSeconds', 28800, 86400)]:
        number = result.setdefault(name, default)
        if isinstance(number, bool) or not isinstance(number, (float, int)) or not 1 <= number <= maximum:
            raise Problem(f'{name} must be between 1 and {maximum}')
    health = result.setdefault('healthPath', '/')
    if not isinstance(health, str) or not health.startswith('/') or health.startswith('//') or len(health) > 240 or any(ord(c) < 32 for c in health) or urlsplit(health).netloc:
        raise Problem('Health path must be a local URL path')
    if len((json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode()) > MAX_CONFIG:
        raise Problem('Runtime settings are too large')
    return result


def tool_path(name):
    # Tool discovery is separate from the environment supplied to project code.
    # Local macOS launchers commonly have a short PATH; include normal tool homes.
    search = os.pathsep.join(dict.fromkeys([os.environ.get('PATH', ''), '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin']))
    return shutil.which(name, path=search)


def run_environment(folder, port, *, data_root=None, data_environment=None):
    data_root = data_root or folder / 'data'
    env = {key: value for key, value in os.environ.items() if key in {'LANG', 'LC_ALL', 'LC_CTYPE', 'SYSTEMROOT'}}
    env.update(PATH='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
               HOME=str(folder / 'home'), TMPDIR=str(folder / 'tmp'), TMP=str(folder / 'tmp'), TEMP=str(folder / 'tmp'),
               XDG_CONFIG_HOME=str(folder / 'home/config'), XDG_CACHE_HOME=str(folder / 'cache'),
               XDG_DATA_HOME=str(folder / 'data'), UNFORGE_DATA_DIR=str(data_root),
               UNFORGE_LOCAL_TEST='1', UNFORGE_PORT=str(port), PORT=str(port), HOST='127.0.0.1',
               NODE_ENV='development', NO_COLOR='1', CI='1', NODE_OPTIONS='--max-old-space-size=2048',
               MAKEFLAGS='-j2', CMAKE_BUILD_PARALLEL_LEVEL='2', SWIFTPM_MAXIMUM_CONCURRENT_OPERATIONS='2',
               npm_config_cache=str(folder / 'cache/npm'), npm_config_userconfig=os.devnull,
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0', GIT_ATTR_NOSYSTEM='1',
               PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
    for name, relative in (data_environment or {}).items():
        env[name] = str(data_target(data_root, relative))
    return env


class RuntimeService:
    def __init__(self, engine, *, worker_command=None):
        self.engine = engine
        self.home = engine.home / '.runtime'
        if self.home.is_symlink():
            raise Problem('Runtime storage must not be a symbolic link')
        self.home.mkdir(mode=0o700, exist_ok=True)
        self._lock = threading.RLock()
        self._workers = {}
        self._closed = False
        self.worker_command = worker_command or ([sys.executable, '--runtime-worker'] if getattr(sys, 'frozen', False) else [sys.executable, str(Path(__file__).resolve()), '--worker'])
        self._recover()

    def _folder(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch('[a-f0-9]{32}', run_id):
            raise Problem('Unknown runtime attempt')
        folder = self.home / run_id
        if folder.is_symlink() or not folder.is_dir():
            raise Problem('Unknown runtime attempt')
        return folder

    def _records(self):
        self._recover()
        records = []
        for folder in self.home.iterdir():
            if re.fullmatch('[a-f0-9]{32}', folder.name) and folder.is_dir() and not folder.is_symlink():
                try:
                    records.append(self.get_run(folder.name))
                except (OSError, ValueError, KeyError):
                    continue
        return sorted(records, key=lambda record: record['createdAt'], reverse=True)

    def _recover(self):
        # A prior watchdog closes its process group when its parent pipe closes.
        # Never kill a PID from disk: it could now belong to an unrelated process.
        for folder in self.home.iterdir():
            path = folder / 'record.json'
            if folder.is_symlink() or not folder.is_dir() or not re.fullmatch('[a-f0-9]{32}', folder.name):
                continue
            try:
                record = json.loads(path.read_text())
                if folder.name in self._workers and self._workers[folder.name].poll() is None:
                    continue
                if record['status'] not in ACTIVE:
                    continue
                fd = os.open(folder / 'lease', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    record.update(status='interrupted', finishedAt=now(), error='The previous runtime worker stopped before recording a final outcome. This attempt was not restarted.')
                    atomic_json(path, record)
                finally:
                    os.close(fd)
            except (OSError, ValueError, KeyError):
                continue

    def get_run(self, run_id):
        folder = self._folder(run_id)
        path = folder / 'record.json'
        if path.is_symlink() or path.stat().st_size > MAX_CONFIG:
            raise Problem('Invalid runtime receipt')
        result = json.loads(path.read_text())
        log = folder / 'output.log'
        if log.is_file() and not log.is_symlink():
            with log.open('rb') as stream:
                stream.seek(max(0, log.stat().st_size - MAX_LOG))
                result['output'] = stream.read(MAX_LOG).decode('utf-8', errors='replace')
        else:
            result['output'] = ''
        return result

    def get(self, pid):
        with self._lock, self.engine.lock:
            root = self.engine.root(pid)
            path = self.engine.safe_path(root, '.unforge/runtime.json')
            config = None
            revision = None
            if path.exists():
                if not path.is_file() or path.stat().st_size > MAX_CONFIG:
                    raise Problem('Invalid runtime settings file')
                data = path.read_bytes()
                config = validate(json.loads(data))
                revision = hashlib.sha256(data).hexdigest()
            tools = {name: bool(tool_path(name)) for name in TOOLS[config['profile']]} if config else {}
            suggestion = self.suggested_config(pid)
            return dict(config=config, revision=revision, availableTools=tools,
                        suggestedConfig=suggestion['config'], suggestionReason=suggestion['reason'], suggestionNotes=suggestion['notes'],
                        runs=[record for record in self._records() if record['projectId'] == pid][:20],
                        limits=LIMITS, sourcePolicy='Saved versions only; each attempt uses a separate copy.',
                        dataPolicy='Preview uses fresh data. Explicit local use keeps declared app data between runs; project code still runs with your account permissions.')

    def suggested_config(self, pid):
        """Inspect common project entrypoints without executing a command."""
        with self.engine.lock:
            root = self.engine.root(pid)
            def present(name):
                path = self.engine.safe_path(root, name)
                return path.is_file() and path.stat().st_size <= 128 * 1024
            def suggestion(config, reason, notes=None):
                return dict(config=validate(config) if config else None, reason=reason, notes=notes or [])
            if present('package.json'):
                try:
                    package = json.loads((root / 'package.json').read_text())
                    scripts = package.get('scripts', {})
                    if not isinstance(scripts, dict) or any(not isinstance(value, str) for value in scripts.values()):
                        raise ValueError('Invalid scripts')
                    dependencies = set()
                    for section in ('dependencies', 'devDependencies'):
                        declared = package.get(section, {})
                        if isinstance(declared, dict):
                            dependencies.update(declared)
                except (ValueError, AttributeError):
                    return suggestion(None, 'The package.json scripts could not be read. Choose runtime settings explicitly.')
                name = 'dev' if 'dev' in scripts else 'start' if 'start' in scripts else None
                if name is None:
                    return suggestion(None, 'No dev or start script was found. Add a local start command to package.json first.')
                command = ['npm', 'run', name]
                script = scripts[name]
                simple = not any(separator in script for separator in ('&&', '||', ';', '\n', '|'))
                if simple and re.search(r'(^|\s)vite(?:\s|$)', script):
                    command += ['--', '--host', '127.0.0.1', '--port', '{port}', '--strictPort']
                    reason = 'Vite can start on a private local port.'
                elif simple and re.search(r'(^|\s)next\s+(dev|start)(\s|$)', script):
                    command += ['--', '--hostname', '127.0.0.1', '--port', '{port}']
                    reason = 'Next.js can start on a private local port.'
                else:
                    reason = f'Found the {name} script. The app must honor the supplied PORT and HOST environment variables.'
                notes = ['Starting runs the package script and any pre/post script it defines. Review code before trusting it.']
                if len(command) == 3 and dependencies.intersection({'vite', 'next'}):
                    notes.append('A web framework dependency is present, but its start command is wrapped or combined. Port flags were not guessed; confirm that the script uses PORT and HOST.')
                prepare = ['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'] if present('package-lock.json') else []
                if prepare:
                    notes.append('Dependencies will be installed into the disposable copy. Install hooks are disabled; packages requiring native build hooks need explicit settings.')
                else:
                    notes.append('No package-lock.json was found. Dependencies are not installed automatically; this suggestion may require dependency setup.')
                check_name = 'check' if 'check' in scripts else 'test' if 'test' in scripts else None
                check = ['npm', 'run', check_name] if check_name else []
                return suggestion(dict(schemaVersion=1, profile='node', run=command, check=check, prepare=prepare), reason, notes)
            if present('Package.swift'):
                return suggestion(dict(schemaVersion=1, profile='swift', run=['swift', 'run', '--jobs', '2'],
                                       check=['swift', 'test', '--jobs', '2']),
                                  'Found a Swift package. An executable product is required for Run.',
                                  ['This builds on this Mac and may fetch declared package dependencies. Native windows do not appear inside a browser preview.',
                                   'Declare the app data-store setting explicitly. Native frameworks may choose a system application-data location instead of the temporary HOME.'])
            python_entries = [name for name in ('app.py', 'main.py') if present(name)]
            if len(python_entries) > 1:
                return suggestion(None, 'Both app.py and main.py exist. Choose the intended local entrypoint explicitly.')
            if python_entries:
                checks = ['python3', '-m', 'unittest', 'discover', '-v'] if (root / 'tests').is_dir() or any(root.glob('test_*.py')) else []
                notes = ['The app must honor PORT and HOST for a browser preview. A separate data directory is supplied as UNFORGE_DATA_DIR.']
                if present('requirements.txt') or present('pyproject.toml'):
                    notes.append('Python dependencies are not installed automatically. Configure an explicit preparation command if they are required.')
                return suggestion(dict(schemaVersion=1, profile='python', run=['python3', python_entries[0]], check=checks),
                                  f'Found the Python entrypoint {python_entries[0]}.', notes)
            if any(root.glob('*.xcodeproj')) or any(root.glob('*.xcworkspace')):
                return suggestion(None, 'This is a native Xcode app. Select its build scheme and executable explicitly; a generic browser command would be misleading.')
            if present('index.html'):
                return suggestion(dict(schemaVersion=1, profile='static'), 'Found index.html. The built-in local server needs no dependency installation.',
                                  ['The static check only verifies the entry page exists; it does not test browser behavior.'])
            return suggestion(None, 'No unambiguous local entrypoint was found. Choose a project directory and explicit run/check commands.')

    def configure(self, pid, document, expected_revision):
        config = validate(document)
        with self._lock, self.engine.lock:
            current = self.get(pid)
            if current['revision'] != expected_revision:
                raise Problem('Runtime settings changed. Reload before saving again.')
            root = self.engine.root(pid)
            atomic_json(self.engine.safe_path(root, '.unforge/runtime.json'), config)
            return self.get(pid)

    def overview(self):
        with self._lock:
            records = self._records()
            return dict(runs=records[:100], active=[record for record in records if record['status'] in ACTIVE], limits=LIMITS)

    def start(self, pid, trusted=False, persistent=False):
        if not isinstance(persistent, bool):
            raise Problem('Choose preview or persistent local use explicitly')
        return self._start(pid, 'run', trusted, persistent)

    def check(self, pid, trusted=False):
        return self._start(pid, 'check', trusted)

    def _start(self, pid, kind, trusted, persistent=False):
        if trusted is not True:
            raise Problem('Confirm trusted local execution before starting project code. This is not a sandbox.')
        with self._lock, self.engine.lock:
            if self._closed:
                raise Problem('The local runtime supervisor is stopping')
            if len(self._records()) >= MAX_ATTEMPTS:
                raise Problem('The workspace has 100 saved runtime attempts. Remove an older stopped attempt before starting another.')
            settings = self.get(pid)
            config = settings['config']
            if config is None:
                raise Problem('Set up this project runtime before starting it')
            if kind == 'check' and not config['check'] and config['profile'] != 'static':
                raise Problem('Choose an explicit check command first')
            if any(record['status'] in ACTIVE for record in settings['runs']):
                raise Problem('This project already has an active runtime attempt. Stop it first.')
            root = self.engine.root(pid)
            if self.engine.git(root, 'status', '--porcelain'):
                raise Problem('Save the project version, including runtime settings, before running it')
            source_head = self.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
            self.engine.validate_checkout_size(root, source_head)
            self.engine.validate_tree(root, source_head)
            resolved = {}
            for name in (kind, 'prepare'):
                if config[name]:
                    executable = tool_path(config[name][0])
                    if executable is None:
                        raise Problem(f'Install {config[name][0]} to use this project runtime')
                    resolved[name] = [executable, *config[name][1:]]
            run_id = uuid.uuid4().hex
            folder = self.home / run_id
            folder.mkdir(mode=0o700)
            try:
                candidate = folder / 'candidate'
                candidate.mkdir()
                archive = folder / 'source.tar'
                archive.write_bytes(self.engine.git(root, 'archive', '--format=tar', source_head))
                with tarfile.open(archive) as bundle:
                    # Tree and bounds were validated above. Reject links again at the extraction boundary.
                    for member in bundle:
                        parts = Path(member.name).parts
                        if Path(member.name).is_absolute() or '..' in parts or not (member.isfile() or member.isdir()):
                            raise Problem('Runtime source must contain only ordinary files and directories')
                        target = candidate / member.name
                        if member.isdir():
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with bundle.extractfile(member) as reader, target.open('wb') as writer:
                                shutil.copyfileobj(reader, writer)
                            target.chmod(0o700 if member.mode & 0o111 else 0o600)
                archive.unlink()
                for name in ('home', 'data', 'tmp', 'cache'):
                    (folder / name).mkdir(mode=0o700)
                if persistent:
                    storage = self.engine.home / '.app-data'
                    if storage.is_symlink():
                        raise Problem('Persistent app data must not use a symbolic link')
                    storage.mkdir(mode=0o700, exist_ok=True)
                    data_root = data_target(storage, pid)
                    data_root.mkdir(mode=0o700, exist_ok=True)
                else:
                    data_root = folder / 'data'
                for relative in config['dataPaths']:
                    # Mounts are project-root-relative, regardless of the command's cwd.
                    mount = self.engine.safe_path(candidate, relative)
                    if mount.exists() or mount.is_symlink():
                        raise Problem(f'Data directory {relative} collides with saved source. Move the data out of source before mounting it.')
                    target = data_target(data_root, relative)
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    mount.parent.mkdir(parents=True, exist_ok=True)
                    mount.symlink_to(target, target_is_directory=True)
                for relative in config['dataEnvironment'].values():
                    data_target(data_root, relative).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                cwd = candidate / config['directory']
                if not cwd.is_dir() or cwd.is_symlink():
                    raise Problem('The configured runtime directory does not exist in the saved source')
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    port = sock.getsockname()[1]
                record = dict(id=run_id, projectId=pid, kind=kind, profile=config['profile'], status='starting',
                              sourceHead=source_head, configRevision=settings['revision'], createdAt=now(), finishedAt=None,
                              exitCode=None, error=None, port=port if kind == 'run' else None,
                              url=f'http://127.0.0.1:{port}' if kind == 'run' else None,
                              health='not-checked', healthNote='A local HTTP response is observed separately from application correctness.',
                              outputTruncated=False, candidatePath=str(candidate), dataPath=str(folder / 'data'),
                              persistent=persistent,
                              dataPolicy=('UNFORGE_DATA_DIR and configured data mounts/variables persist across runs. Other candidate files remain temporary.' if persistent else 'This attempt uses fresh disposable data. Removing the attempt removes that data.'),
                              trustedLocal=True, limits=LIMITS)
                record['dataPath'] = str(data_root)
                atomic_json(folder / 'record.json', record)
                atomic_json(folder / 'manifest.json', dict(record=record, config=config, commands=resolved, port=port))
                with (folder / 'worker-error.log').open('wb') as errors:
                    process = subprocess.Popen([*self.worker_command, str(folder / 'manifest.json')],
                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors,
                        env=run_environment(folder, port), cwd=folder, start_new_session=True)
                self._workers[run_id] = process
                return self.get_run(run_id)
            except Exception:
                shutil.rmtree(folder, ignore_errors=True)
                raise

    def stop(self, pid, run_id=None):
        self.engine.root(pid)
        with self._lock:
            records = [self.get_run(run_id)] if run_id else [record for record in self._records() if record['projectId'] == pid and record['status'] in ACTIVE]
            for record in records:
                if record['projectId'] != pid:
                    raise Problem('Runtime attempt belongs to another project')
                if record['status'] in ACTIVE:
                    (self._folder(record['id']) / 'stop').touch(mode=0o600)
            return [self.get_run(record['id']) for record in records]

    def remove(self, pid, run_id):
        """Explicitly remove a stopped attempt and its disposable source/data/logs."""
        self.engine.root(pid)
        with self._lock:
            record = self.get_run(run_id)
            if record['projectId'] != pid:
                raise Problem('Runtime attempt belongs to another project')
            if record['status'] in ACTIVE:
                raise Problem('Stop the runtime attempt before removing its files')
            process = self._workers.get(run_id)
            if process and process.poll() is None:
                raise Problem('Wait for process cleanup before removing this attempt')
            if process and process.stdin and not process.stdin.closed:
                process.stdin.close()
            shutil.rmtree(self._folder(run_id))
            self._workers.pop(run_id, None)
            return dict(removed=run_id)

    def close(self):
        with self._lock:
            self._closed = True
            workers = list(self._workers.items())
            for run_id, process in workers:
                if process.poll() is None:
                    (self._folder(run_id) / 'stop').touch(mode=0o600)
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
        for run_id, process in workers:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # Only our actual Popen child is targeted, never a PID recovered from disk.
                    process.kill()
                    process.wait(timeout=5)
        self._recover()


class StaticHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
            self.send_error(403)
            return
        parts = unquote(urlsplit(self.path).path).split('/')
        if any(part in ('.', '..') or part.startswith('.') or '\\' in part for part in parts if part):
            self.send_error(404)
            return
        root = self.server.source
        path = root.joinpath(*[part for part in parts if part])
        if path.is_dir():
            path /= 'index.html'
        if not path.resolve().is_relative_to(root.resolve()) or any(parent.is_symlink() for parent in [path, *path.parents] if parent.is_relative_to(root)) or not path.is_file():
            self.send_error(404)
            return
        size = path.stat().st_size
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(size))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            with path.open('rb') as stream:
                shutil.copyfileobj(stream, self.wfile)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


def heavy_lock():
    path = Path('/tmp') / f'unforge-runtime-heavy-{os.getuid()}.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise Problem('Runtime scheduler lock is not owned by this account')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise Problem('Another local check or dependency preparation is running. Wait for it to finish.')
    return fd


def persistent_data_lock(folder, project_id):
    """The application inherits this lease so a lost watchdog cannot unlock its data."""
    path = folder.parent / f'data-{project_id}.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise Problem('Persistent app data lock is not owned by this account')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise Problem('This app data is still in use by another process. Close the earlier app before starting local use again.')
    return fd


def terminate_process_group(child):
    """Stop an owned child group, including macOS's transient exit/EPERM race."""
    if child is None:
        return

    def send(sig):
        for attempt in range(5):
            # A group can disappear while its direct child is still becoming
            # waitable. Reaping once before killpg does not close that race.
            child.poll()
            try:
                os.killpg(child.pid, sig)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                if child.poll() is not None:
                    inspection = subprocess.run(['ps', '-o', 'stat=', '-g', str(child.pid)],
                                                capture_output=True, text=True, timeout=1)
                    # ps returns 1 for an empty selection. Inspection failures
                    # must not be mistaken for evidence that the group is gone.
                    if inspection.returncode not in (0, 1) or inspection.stderr.strip():
                        raise
                    states = inspection.stdout.split()
                    if all(state.startswith('Z') for state in states):
                        return False
                if attempt == 4:
                    raise
                # Retry only this owned group. A persistent denial or a live
                # member still fails cleanup; neither is reported as success.
                time.sleep(.02)

    send(signal.SIGTERM)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and send(0):
        time.sleep(.03)
    send(signal.SIGKILL)
    child.wait(timeout=5)


def worker_main(manifest_path):
    """Private subprocess entrypoint. Parent-pipe EOF stops the owned process group."""
    folder = Path(manifest_path).resolve().parent
    manifest = json.loads((folder / 'manifest.json').read_text())
    record, config = manifest['record'], validate(manifest['config'])
    path = folder / 'record.json'
    stopped = threading.Event()
    parent_lost = threading.Event()
    lease = os.open(folder / 'lease', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lease, fcntl.LOCK_EX)
    record['workerPid'] = os.getpid()
    atomic_json(path, record)
    def parent_watch():
        try:
            while os.read(sys.stdin.fileno(), 1):
                pass
        finally:
            parent_lost.set()
            stopped.set()
    threading.Thread(target=parent_watch, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    class LocalHealthOnly(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    health_client = build_opener(ProxyHandler({}), LocalHealthOnly())
    process = None
    lock = None
    data_lock = None
    server = None
    log = bytearray()
    def output(data):
        log.extend(data)
        if len(log) > MAX_LOG:
            del log[:-MAX_LOG]
            record['outputTruncated'] = True
        target = folder / 'output.log'
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(log)
    def stop_requested():
        return stopped.is_set() or (folder / 'stop').exists()
    def command(name, timeout):
        nonlocal process
        argv = [arg.replace('{port}', str(manifest['port'])) for arg in manifest['commands'][name]]
        env = run_environment(folder, manifest['port'], data_root=Path(record['dataPath']), data_environment=config['dataEnvironment'])
        env['PATH'] = str(Path(argv[0]).parent) + os.pathsep + env['PATH']
        process = subprocess.Popen(argv, cwd=folder / 'candidate' / config['directory'], env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True, pass_fds=(data_lock,) if data_lock is not None else ())
        record['processPid'] = process.pid
        atomic_json(path, record)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        health_deadline = time.monotonic() + config['startupTimeoutSeconds']
        next_health = time.monotonic()
        try:
            while True:
                for key, _ in selector.select(.1):
                    data = os.read(key.fileobj.fileno(), 8192)
                    if data:
                        output(data)
                    else:
                        selector.unregister(key.fileobj)
                if stop_requested():
                    return 'interrupted' if parent_lost.is_set() else 'stopped', None
                if time.monotonic() >= deadline:
                    return 'timed-out', None
                code = process.poll()
                if code is not None:
                    # Drain any immediately available final output without waiting on descendant pipes.
                    for key, _ in selector.select(0):
                        data = os.read(key.fileobj.fileno(), 8192)
                        if data:
                            output(data)
                    return ('passed' if code == 0 else 'failed'), code
                if name == 'run' and time.monotonic() >= next_health:
                    next_health = time.monotonic() + 2
                    try:
                        with health_client.open(record['url'] + config['healthPath'], timeout=.3) as response:
                            healthy = 200 <= response.status < 400
                        record['health'] = 'responding' if healthy else 'not-responding'
                    except Exception:
                        record['health'] = 'not-responding' if time.monotonic() >= health_deadline else 'starting'
                    atomic_json(path, record)
        finally:
            selector.close()
            terminate_process_group(process)
            process.stdout.close()
            process = None
    try:
        if record.get('persistent'):
            data_lock = persistent_data_lock(folder, record['projectId'])
        if record['kind'] == 'check' or config['prepare']:
            lock = heavy_lock()
        if stop_requested():
            record['status'] = 'interrupted' if parent_lost.is_set() else 'stopped'
        else:
            if config['prepare']:
                record['status'] = 'preparing'
                atomic_json(path, record)
                outcome, code = command('prepare', config['checkTimeoutSeconds'])
                if outcome != 'passed':
                    record.update(status=outcome, exitCode=code, error='Dependency preparation did not complete successfully.')
                    return 0
            if record['kind'] == 'run' and lock is not None:
                os.close(lock)
                lock = None
            record['status'] = 'running'
            atomic_json(path, record)
            if config['profile'] == 'static':
                source = folder / 'candidate' / config['directory']
                if not (source / 'index.html').is_file():
                    raise Problem('The static runtime directory needs index.html')
                if record['kind'] == 'check':
                    record.update(status='passed', exitCode=0)
                    output(b'Static check: index.html exists in the saved candidate. This is not a browser behavior test.\n')
                else:
                    server = ThreadingHTTPServer(('127.0.0.1', manifest['port']), StaticHandler)
                    server.source = source
                    server.timeout = .2
                    record['health'] = 'responding'
                    atomic_json(path, record)
                    deadline = time.monotonic() + config['runTimeoutSeconds']
                    while not stop_requested() and time.monotonic() < deadline:
                        server.handle_request()
                    record['status'] = ('interrupted' if parent_lost.is_set() else 'stopped') if stop_requested() else 'timed-out'
            else:
                name = record['kind']
                outcome, code = command(name, config['runTimeoutSeconds'] if name == 'run' else config['checkTimeoutSeconds'])
                record.update(status=outcome, exitCode=code)
                if outcome == 'timed-out':
                    record['error'] = 'The configured execution time limit was reached.'
    except Exception as exc:
        record.update(status='failed', error=str(exc)[:2000])
    finally:
        if server:
            server.server_close()
        try:
            terminate_process_group(process)
        except OSError as exc:
            record.update(status='failed', error='Could not verify process cleanup: ' + str(exc))
        if lock is not None:
            os.close(lock)
        if data_lock is not None:
            os.close(data_lock)
        record.update(finishedAt=now(), processPid=None)
        atomic_json(path, record)
        os.close(lease)
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', required=True)
    sys.exit(worker_main(parser.parse_args().worker))
