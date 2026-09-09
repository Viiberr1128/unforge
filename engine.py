#!/usr/bin/env python3
"""Unforge's local-only, dependency-free project engine."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import unicodedata
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote

MAX_BODY = 512 * 1024
MAX_TEXT = 128 * 1024
UNSET = object()

class Problem(ValueError):
    pass

class Engine:
    def __init__(self, home):
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def git(self, root, *args, allowed_returncodes=(0,)):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT='0', GIT_ATTR_NOSYSTEM='1')
        result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
            '-c', 'user.name=Unforge Local', '-c', 'user.email=local@unforge.invalid',
            '-c', 'commit.gpgsign=false', *args], cwd=root, env=env, capture_output=True, timeout=30)
        if result.returncode not in allowed_returncodes:
            raise Problem(result.stderr.decode(errors='replace').strip() or 'Git operation failed')
        return result.stdout

    def root(self, pid):
        if not isinstance(pid, str) or not re.fullmatch(r'[a-f0-9]{32}', pid):
            raise Problem('Unknown project')
        root = self.home / pid
        if root.is_symlink() or not root.is_dir() or not (root / '.unforge' / 'project.json').is_file():
            raise Problem('Unknown project')
        # Reject a replaced git directory before Git can follow it elsewhere.
        if (root / '.git').is_symlink() or not (root / '.git').is_dir():
            raise Problem('Invalid project repository')
        self.metadata(root)
        return root

    def metadata(self, root):
        marker = root / '.unforge' / 'project.json'
        if marker.is_symlink() or marker.parent.is_symlink():
            raise Problem('Invalid project metadata')
        data = json.loads(marker.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('name'), str) or not isinstance(data.get('description'), str):
            raise Problem('Invalid project metadata')
        return data

    def projects(self):
        items = []
        for child in sorted(self.home.iterdir()):
            try:
                root = self.root(child.name)
                data = self.metadata(root)
                items.append(dict(id=child.name, name=data['name'], description=data['description'], path=str(root), updatedAt=self.history(root)[0]['date']))
            except (Problem, OSError, ValueError, KeyError):
                continue
        return items

    def create(self, name, description=''):
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise Problem('Name must contain 1–120 characters')
        if not isinstance(description, str) or len(description) > 4000:
            raise Problem('Description is too long')
        with self.lock:
            pid = uuid.uuid4().hex
            root = self.home / pid
            root.mkdir()
            (root / '.unforge').mkdir()
            (root / '.unforge' / 'project.json').write_text(json.dumps(dict(name=name.strip(), description=description, schemaVersion=1)), encoding='utf-8')
            self.git(root, 'init', '--template=', '-b', 'main')
            (root / 'README.md').write_text(f'# {name.strip()}\n\n{description}\n', encoding='utf-8')
            import html
            (root / 'index.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + html.escape(name) + '</title><style>body{font:18px system-ui;background:#f6f4ef;color:#25372f;padding:8vw;max-width:800px}h1{font-size:3rem}p{line-height:1.7}</style><h1>' + html.escape(name) + '</h1><p>' + html.escape(description or 'Your locally owned project starts here.') + '</p></html>', encoding='utf-8')
            self.git(root, 'add', '--', 'README.md', 'index.html', '.unforge/project.json')
            self.git(root, 'commit', '-m', 'Create project')
            return self.detail(pid)

    def import_bundle(self, path, name=None):
        if not isinstance(path, str) or not Path(path).is_absolute() or Path(path).suffix.lower() != '.bundle':
            raise Problem('Choose an absolute local .bundle file path')
        source = Path(path)
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 512 * 1024 * 1024:
            raise Problem('Bundle must be a regular file smaller than 512 MiB')
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 120):
            raise Problem('Name must contain 1–120 characters')
        with self.lock, tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / 'import.bundle'
            # Snapshot the input before verifying it; never execute in its directory.
            with source.open('rb') as reader, copied.open('wb') as writer:
                if reader.readline(64) not in (b'# v2 git bundle\n', b'# v3 git bundle\n'):
                    raise Problem('Not a supported Git bundle')
                reader.seek(0)
                remaining = 512 * 1024 * 1024 + 1
                while remaining:
                    chunk = reader.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    writer.write(chunk)
                    remaining -= len(chunk)
                if remaining == 0:
                    raise Problem('Bundle is too large')
            pid = uuid.uuid4().hex
            root = self.home / pid
            root.mkdir()
            try:
                self.git(root, 'init', '--template=', '-b', 'main')
                self.git(root, 'bundle', 'verify', str(copied))
                heads = self.git(root, 'bundle', 'list-heads', str(copied)).decode('utf-8').splitlines()
                refs = dict(line.split(' ', 1)[::-1] for line in heads)
                branches = sorted(ref for ref in refs if ref.startswith('refs/heads/'))
                if not branches:
                    raise Problem('Bundle must contain a complete local branch')
                matching = [ref for ref in branches if refs[ref] == refs.get('HEAD')]
                branch = ('refs/heads/main' if 'refs/heads/main' in matching else matching[0]) if matching else ('refs/heads/main' if 'refs/heads/main' in branches else branches[0])
                # Fetch every exported ref, preserving other branches and tags too.
                self.git(root, 'fetch', '--update-head-ok', '--no-tags', str(copied), '+refs/*:refs/*')
                self.git(root, 'symbolic-ref', 'HEAD', branch)
                self.validate_checkout_size(root, 'HEAD')
                metadata = self.validate_tree(root, 'HEAD', require_metadata=False)
                self.git(root, 'reset', '--hard', 'HEAD')
                metadata = metadata or dict(name='Imported project', description='Imported from a portable Git bundle', schemaVersion=1)
                if name is not None:
                    metadata['name'] = name.strip()
                metadata['schemaVersion'] = 1
                (root / '.unforge').mkdir(exist_ok=True)
                marker = root / '.unforge' / 'project.json'
                marker.write_text(json.dumps(metadata), encoding='utf-8')
                self.git(root, 'add', '--', '.unforge/project.json')
                if self.git(root, 'diff', '--cached', '--name-only'):
                    self.git(root, 'commit', '-m', 'Record local project import')
                return self.detail(pid)
            except Exception:
                shutil.rmtree(root)
                raise

    def safe_path(self, root, path):
        if not isinstance(path, str) or not path or len(path) > 240 or '\\' in path or any(ord(c) < 32 for c in path):
            raise Problem('Invalid file path')
        parts = path.split('/')
        if any(p in ('', '.', '..') for p in parts) or Path(path).is_absolute():
            raise Problem('Invalid file path')
        for part in parts:
            low = part.lower()
            if low in ('.git', '.gitattributes', '.gitmodules', '.gitconfig') or low.startswith('.env') or low == '.unforge-project.json' or low in ('credentials', 'secrets') or low.startswith(('credentials.', 'secrets.')) or low.endswith(('.pem', '.key', '.p12', '.pfx')):
                raise Problem('Private or reserved file path')
        target = root
        for part in parts:
            target = target / part
            if target.is_symlink():
                raise Problem('Symbolic links are not supported')
        if not target.resolve().is_relative_to(root.resolve()):
            raise Problem('File path escapes project')
        return target

    def history(self, root):
        raw = self.git(root, 'log', '-100', '-z', '--format=%H%x00%s%x00%cI').decode('utf-8', errors='replace').rstrip('\0')
        records = raw.split('\0') if raw else []
        if not records or len(records) % 3:
            raise Problem('Project history is unavailable or malformed')
        return [dict(zip(('id', 'message', 'date'), records[i:i + 3])) for i in range(0, len(records), 3)]

    def detail(self, pid):
        with self.lock:
            root = self.root(pid)
            data = self.metadata(root)
            files = []
            names = self.git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').decode().split('\0')
            total = 0
            for name in sorted(set(names)):
                if not name:
                    continue
                try:
                    target = self.safe_path(root, name)
                    if not target.is_file() or target.stat().st_size > MAX_TEXT:
                        continue
                    content = target.read_text(encoding='utf-8')
                    if '\0' in content:
                        continue
                    total += len(content.encode())
                    if total > 2 * 1024 * 1024 or len(files) >= 100:
                        break
                    files.append(dict(path=name, content=content, readonly=name.lower() == '.unforge/project.json'))
                except (Problem, OSError, UnicodeError):
                    continue
            diff = self.git(root, 'diff', '--no-ext-diff', '--no-textconv', 'HEAD', '--').decode(errors='replace')[:512 * 1024]
            return dict(id=pid, name=data['name'], description=data['description'], path=str(root), files=files,
                        history=self.history(root), diff=diff, dirty=bool(self.git(root, 'status', '--porcelain')))

    def edit(self, pid, path, content, expected_content=UNSET):
        if isinstance(path, str) and '/.unforge/project.json' in '/' + path.lower():
            raise Problem('Project metadata is managed by Unforge')
        if not isinstance(content, str) or len(content.encode()) > MAX_TEXT or '\0' in content:
            raise Problem('File must be text smaller than 128 KiB')
        with self.lock:
            root = self.root(pid)
            target = self.safe_path(root, path)
            # check-ignore respects the index: tracked files remain editable even
            # when a later ignore rule matches them. Refuse an untracked ignored
            # path before a write could vanish from the UI and exported history.
            if self.git(root, 'check-ignore', '--', path, allowed_returncodes=(0, 1)):
                raise Problem('This file is excluded from saved versions by a Git ignore rule. Choose a different path or update .gitignore before writing it.')
            if expected_content is not UNSET:
                if expected_content is None:
                    if target.exists():
                        raise Problem('This file already exists. Reload it before saving.')
                elif isinstance(expected_content, str):
                    if not target.is_file() or target.read_text(encoding='utf-8') != expected_content:
                        raise Problem('This file changed outside your editor. Reload it before saving.')
                else:
                    raise Problem('expectedContent must be text or null for a new file')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
            return self.detail(pid)

    def save(self, pid, message):
        if not isinstance(message, str) or not message.strip() or len(message) > 500:
            raise Problem('Version description must contain 1–500 characters')
        with self.lock:
            root = self.root(pid)
            # Validate all changed paths, including edits made outside the UI.
            paths = self.git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').decode().split('\0')
            for path in filter(None, paths):
                self.safe_path(root, path)
            self.git(root, 'add', '-A', '--', '.')
            if self.git(root, 'diff', '--cached', '--name-only'):
                self.git(root, 'commit', '-m', message.strip())
            return self.detail(pid)

    def validate_checkout_size(self, root, revision):
        total = 0
        count = 0
        for record in self.git(root, 'ls-tree', '-rlz', revision).split(b'\0'):
            if not record:
                continue
            header = record.split(b'\t', 1)[0].split()
            count += 1
            if len(header) == 4 and header[3] != b'-':
                total += int(header[3])
            if count > 10000 or total > 128 * 1024 * 1024:
                raise Problem('Imported checkout exceeds 128 MiB or 10,000 files')

    def validate_tree(self, root, revision, require_metadata=True):
        target_paths = []
        portable_paths = {}
        for record in self.git(root, 'ls-tree', '-rz', revision).decode('utf-8').split('\0'):
            if not record:
                continue
            header, path = record.split('\t', 1)
            mode, kind, _ = header.split(' ', 2)
            if mode not in ('100644', '100755') or kind != 'blob':
                raise Problem('Restore cannot include symbolic links or submodules')
            self.safe_path(root, path)
            # Verify every directory prefix too: a Linux tree containing a file
            # "Data" and a directory "data" cannot be checked out losslessly on
            # the usual macOS filesystem. Reject before Git writes any files.
            parts = path.split('/')
            for index in range(1, len(parts) + 1):
                prefix = '/'.join(parts[:index])
                kind = 'file' if index == len(parts) else 'directory'
                key = unicodedata.normalize('NFC', prefix).casefold()
                prior = portable_paths.get(key)
                if prior and (prior != (prefix, kind) or kind == 'file'):
                    raise Problem('Version contains paths that collide by case or Unicode normalization')
                portable_paths[key] = (prefix, kind)
            if path.casefold() == '.unforge/project.json' and path != '.unforge/project.json':
                raise Problem('Project metadata path must use its standard spelling')
            target_paths.append(path)
        if '.unforge/project.json' not in target_paths:
            if require_metadata:
                raise Problem('Version does not contain project metadata')
            return None
        try:
            metadata = json.loads(self.git(root, 'show', revision + ':.unforge/project.json').decode('utf-8'))
            if not isinstance(metadata, dict) or not isinstance(metadata.get('name'), str) or not isinstance(metadata.get('description'), str):
                raise ValueError()
        except ValueError:
            raise Problem('Version contains invalid project metadata')
        return metadata

    def validate_restore(self, root, revision):
        self.validate_tree(root, revision)
        target_paths = self.git(root, 'ls-tree', '-rz', '--name-only', revision).decode('utf-8').split('\0')
        # Be conservative across filesystems: macOS commonly treats case and
        # Unicode normalization variants as the same file. A harmless refusal on
        # a case-sensitive filesystem is preferable to overwriting ignored work.
        def collision_key(path):
            return unicodedata.normalize('NFC', path).casefold()
        target_keys = {collision_key(path) for path in target_paths if path}
        # Include ignored files: Git restore may otherwise overwrite these silently.
        for path in filter(None, self.git(root, 'ls-files', '--others', '-z').decode('utf-8').split('\0')):
            key = collision_key(path)
            if any(key == target or key.startswith(target + '/') or target.startswith(key + '/') for target in target_keys):
                raise Problem('Restore would overwrite an untracked or ignored file: ' + path)
        for path in filter(None, self.git(root, 'ls-files', '-z').decode('utf-8').split('\0')):
            self.safe_path(root, path)

    def restore(self, pid, revision):
        with self.lock:
            root = self.root(pid)
            if not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{40,64}', revision) or revision not in [h['id'] for h in self.history(root)]:
                raise Problem('Choose a version from this project history')
            if self.git(root, 'status', '--porcelain'):
                raise Problem('Save your current changes before restoring a version')
            self.validate_restore(root, revision)
            self.git(root, 'restore', '--source', revision, '--staged', '--worktree', '--', '.')
            self.git(root, 'commit', '--allow-empty', '-m', f'Restore version {revision[:12]}')
            return self.detail(pid)

    def request(self, pid, message):
        if not isinstance(message, str) or not message.strip() or len(message) > 8000:
            raise Problem('Request must contain 1–8000 characters')
        path = f'.unforge/requests/{uuid.uuid4().hex}.md'
        return self.edit(pid, path, '# Change request\n\n' + message.strip() + '\n\nThis request is ready for handoff to your coding agent. It has not been executed.\n')

    def export(self, pid):
        with self.lock:
            root = self.root(pid)
            with tempfile.TemporaryDirectory() as temp:
                bundle = Path(temp) / 'project.bundle'
                self.git(root, 'bundle', 'create', str(bundle), '--all')
                return bundle.read_bytes()

    def insights(self, pid):
        from insights import analyze
        with self.lock:
            root = self.root(pid)
            names = self.git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').decode('utf-8', errors='replace').split('\0')
            candidates = sorted({name for name in names if name and Path(name).suffix.lower() in ('.json', '.jsonc', '.toml', '.yaml', '.yml')}, key=lambda name: (Path(name).name != 'package.json', name))
            files = []
            for name in candidates[:200]:
                try:
                    target = self.safe_path(root, name)
                    if target.is_file() and target.stat().st_size <= MAX_TEXT:
                        files.append({'path': name, 'content': target.read_text(encoding='utf-8')})
                except (Problem, OSError, UnicodeError):
                    continue
            report = analyze(files)
            report['limits'].append(f'Inspected {len(files)} configuration files; at most 200 candidates and 128 KiB per file. This is not a full code or account audit.')
            return report

class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, engine, static):
        self.engine, self.static, self.token = engine, Path(static).resolve(), secrets.token_urlsafe(32)
        lock_path = engine.home / '.server.lock'
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self._home_lock = os.fdopen(fd, 'a')
        try:
            fcntl.flock(self._home_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._home_lock.close()
            self._home_lock = None
            raise Problem('Another Unforge server is already using this data folder')
        try:
            super().__init__(address, Handler)
            from agent_jobs import AgentJobs
            from operations import Operations
            from recovery import Recovery
            from care import Care
            self.operations = Operations(engine.home)
            self.recovery = Recovery(engine)
            self.care = Care(engine)
            self.agent_jobs = AgentJobs(engine, operations=self.operations)
        except Exception:
            if hasattr(self, 'operations'):
                self.operations.close()
            super().server_close()
            self._release_home_lock()
            raise

    def _release_home_lock(self):
        if self._home_lock is not None:
            fcntl.flock(self._home_lock.fileno(), fcntl.LOCK_UN)
            self._home_lock.close()
            self._home_lock = None

    def server_close(self):
        try:
            if hasattr(self, 'agent_jobs'):
                self.agent_jobs.close()
            if hasattr(self, 'operations'):
                self.operations.close()
            super().server_close()
        finally:
            self._release_home_lock()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, data, mime='application/json', disposition=None):
        if mime == 'application/json':
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
        if disposition:
            self.send_header('Content-Disposition', disposition)
        self.end_headers()
        self.wfile.write(data)

    def host_ok(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, dict(error='Invalid host'))
        try:
            path = unquote(urlsplit(self.path).path)
            if path == '/api/health':
                return self.send(200, dict(ok=True, version='0.2.0'))
            if path == '/api/session':
                return self.send(200, dict(token=self.server.token))
            if path == '/api/agent/status':
                return self.send(200, self.server.agent_jobs.status())
            if path == '/api/operations':
                return self.send(200, self.server.operations.overview())
            consequence_match = re.fullmatch(r'/api/agent/jobs/([a-f0-9]{32})/consequences', path)
            if consequence_match:
                job = self.server.agent_jobs.get(consequence_match[1])
                return self.send(200, self.server.care.consequences(job['projectId'], job.get('diff') or ''))
            job_match = re.fullmatch(r'/api/agent/jobs/([a-f0-9]{32})', path)
            if job_match:
                return self.send(200, self.server.agent_jobs.get(job_match[1]))
            if path == '/api/projects':
                return self.send(200, dict(projects=self.server.engine.projects()))
            recovery_match = re.fullmatch(r'/api/projects/([a-f0-9]{32})/recovery/([a-f0-9]{32})/download', path)
            if recovery_match:
                name, data = self.server.recovery.download(*recovery_match.groups())
                return self.send(200, data, 'application/gzip', f'attachment; filename="{name}"')
            care_match = re.fullmatch(r'/api/projects/([a-f0-9]{32})/(care|consequences|simplify|handoff|retirement|recovery)', path)
            if care_match:
                pid, action = care_match.groups()
                if action == 'handoff':
                    return self.send(200, self.server.care.handoff(pid).encode('utf-8'), 'text/markdown; charset=utf-8', 'attachment; filename="unforge-handoff.md"')
                if action == 'care': result = self.server.care.get(pid)
                elif action == 'consequences': result = self.server.care.consequences(pid)
                elif action == 'simplify': result = self.server.care.simplify_request(pid)
                elif action == 'retirement': result = self.server.care.prepare_retirement(pid)
                else: result = self.server.recovery.state(pid)
                return self.send(200, result)
            match = re.fullmatch(r'/api/projects/([a-f0-9]{32})(/export|/insights)?', path)
            if match:
                if match[2] == '/insights':
                    return self.send(200, self.server.engine.insights(match[1]))
                if match[2] == '/export':
                    return self.send(200, self.server.engine.export(match[1]), 'application/octet-stream', 'attachment; filename="unforge-project.bundle"')
                return self.send(200, self.server.engine.detail(match[1]))
            if path.startswith('/api/'):
                return self.send(404, dict(error='Unknown endpoint'))
            target = self.server.static / path.lstrip('/')
            if '..' in Path(path).parts or not target.resolve().is_relative_to(self.server.static):
                return self.send(403, dict(error='Invalid path'))
            current = target
            while current != self.server.static:
                if current.is_symlink():
                    return self.send(403, dict(error='Symbolic links are not served'))
                current = current.parent
            if not target.is_file():
                target = self.server.static / 'index.html'
            if not target.is_file() or target.is_symlink():
                return self.send(404, dict(error='Build the frontend first'))
            import mimetypes
            return self.send(200, target.read_bytes(), mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
        except (Problem, OSError, ValueError, RecursionError, subprocess.SubprocessError) as error:
            return self.send(400, dict(error=str(error)))

    def do_POST(self):
        expected = f'http://{self.headers.get("Host", "")}'
        if not self.host_ok() or self.headers.get('Origin') != expected or self.headers.get('X-Unforge-Token') != self.server.token:
            return self.send(403, dict(error='Local session authorization required'))
        if urlsplit(self.path).path in ('/api/import-bundle', '/api/import-capsule'):
            capsule = urlsplit(self.path).path == '/api/import-capsule'
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/octet-stream' or self.headers.get('Transfer-Encoding'):
                return self.send(415, dict(error='Binary bundle upload required'))
            try:
                length = int(self.headers.get('Content-Length', '0'))
                limit = (256 if capsule else 20) * 1024 * 1024
                if not 1 <= length <= limit:
                    raise Problem(f'Upload must be between 1 byte and {limit // (1024 * 1024)} MiB')
                with tempfile.TemporaryDirectory() as temporary:
                    bundle = Path(temporary) / ('upload.tar.gz' if capsule else 'upload.bundle')
                    with bundle.open('wb') as output:
                        remaining = length
                        while remaining:
                            chunk = self.rfile.read(min(65536, remaining))
                            if not chunk:
                                raise Problem('Incomplete upload')
                            output.write(chunk)
                            remaining -= len(chunk)
                    result = self.server.recovery.import_capsule(str(bundle)) if capsule else self.server.engine.import_bundle(str(bundle))
                return self.send(201, result)
            except (Problem, OSError, ValueError, RecursionError, subprocess.SubprocessError) as error:
                return self.send(400, dict(error=str(error)))
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json' or self.headers.get('Transfer-Encoding'):
            return self.send(415, dict(error='JSON requests required'))
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > MAX_BODY:
                raise Problem('Invalid request size')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise Problem('JSON object required')
            path = urlsplit(self.path).path
            engine = self.server.engine
            if path == '/api/agent/jobs':
                return self.send(201, self.server.agent_jobs.start(payload.get('projectId'), payload.get('request'), payload.get('operationId')))
            if path == '/api/operations/settings':
                return self.send(200, self.server.operations.configure(payload.get('dailyLimit'), payload.get('expectedRevision')))
            if path == '/api/operations/practice':
                engine.root(payload.get('projectId'))
                return self.send(200, self.server.operations.practice(payload.get('projectId'), payload.get('operationId'), payload.get('action'), payload.get('payload', {})))
            if path == '/api/operations/resume':
                engine.root(payload.get('projectId'))
                return self.send(200, self.server.operations.resume(payload.get('projectId')))
            if path == '/api/operations/reconcile':
                return self.send(200, self.server.operations.reconcile(payload.get('operationId'), payload.get('outcome'), payload.get('note')))
            if path == '/api/recovery/import':
                return self.send(201, self.server.recovery.import_capsule(payload.get('path')))
            job_match = re.fullmatch(r'/api/agent/jobs/([a-f0-9]{32})/(cancel|apply)', path)
            if job_match:
                action = getattr(self.server.agent_jobs, job_match[2])
                return self.send(200, action(job_match[1]))
            if path == '/api/import':
                return self.send(201, engine.import_bundle(payload.get('path'), payload.get('name')))
            if path == '/api/projects':
                return self.send(201, engine.create(payload.get('name'), payload.get('description', '')))
            recovery_match = re.fullmatch(r'/api/projects/([a-f0-9]{32})/recovery(?:/([a-f0-9]{32})/(rehearse|restore))?', path)
            if recovery_match:
                pid, capsule_id, action = recovery_match.groups()
                if action: result = getattr(self.server.recovery, action)(pid, capsule_id)
                else: result = self.server.recovery.create(pid, payload.get('assets', []))
                return self.send(200, result)
            care_match = re.fullmatch(r'/api/projects/([a-f0-9]{32})/(care|retirement|behavior-check)', path)
            if care_match:
                pid, action = care_match.groups()
                if action == 'care':
                    result = self.server.care.save(pid, payload.get('document'), payload.get('expectedRevision'))
                elif action == 'behavior-check':
                    result = self.server.care.record_check(pid, payload.get('example'), payload.get('outcome'), payload.get('note'), payload.get('expectedRevision'))
                else:
                    with engine.lock:
                        evidence = self.server.recovery.state(pid)
                        result = self.server.care.retire(pid, payload.get('expectedRevision'), evidence, payload.get('capsuleId'), payload.get('note', ''))
                return self.send(200, result)
            match = re.fullmatch(r'/api/projects/([a-f0-9]{32})/(file|save|restore|request)', path)
            if not match:
                return self.send(404, dict(error='Unknown endpoint'))
            pid, action = match.groups()
            if action == 'file': result = engine.edit(pid, payload.get('path'), payload.get('content'), payload.get('expectedContent', UNSET))
            elif action == 'save': result = engine.save(pid, payload.get('message'))
            elif action == 'restore': result = engine.restore(pid, payload.get('revision'))
            else: result = engine.request(pid, payload.get('message'))
            return self.send(200, result)
        except (Problem, OSError, ValueError, RecursionError, subprocess.SubprocessError) as error:
            return self.send(400, dict(error=str(error)))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=4319)
    parser.add_argument('--home', default=os.environ.get('UNFORGE_HOME', '~/.local/share/unforge'))
    parser.add_argument('--static', default=str(Path(__file__).parent / 'dist'))
    options = parser.parse_args()
    server = Server(('127.0.0.1', options.port), Engine(options.home), options.static)
    print(f'Unforge: http://127.0.0.1:{options.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
