"""Encrypted, immutable workspace recovery points using the standard restic format.

Active repositories never live in a sync folder. A completed encrypted recovery
point is copied there. Local readback is not evidence that a cloud uploaded it.
"""
from datetime import datetime, timezone
from contextlib import closing, contextmanager
import time
import copy
import selectors
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import uuid

from engine import Problem

MAX_FILES = 250_000
MAX_BYTES = 50 * 1024**3
MIN_STORAGE_RESERVE = 1024**3
MAX_STORAGE_RESERVE = 10 * 1024**3
STORAGE_METADATA_ALLOWANCE = 16 * 1024**2
EXCLUDED = {'.backups', '.runtime', 'node_modules', '.venv', '.venv-macos', '__pycache__',
            '.DS_Store', '.server.lock', 'operation-owners'}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def storage_guard(requirements):
    """Check peak additional disk use, combining destinations on one filesystem.

    This is a conservative preflight, not a disk reservation or cloud quota check.
    Existing recovery points are never removed to make room.
    """
    volumes = {}
    for path, additional, purpose in requirements:
        path = Path(path)
        while not path.exists() and path != path.parent:
            path = path.parent
        try:
            device = path.stat().st_dev
            usage = shutil.disk_usage(path)
        except OSError as error:
            raise Problem('Could not verify free disk space before ' + purpose + '. No bulk copy was started.') from error
        if usage.total <= 0 or usage.free < 0:
            raise Problem('Disk capacity is unavailable. Choose an available backup destination before continuing.')
        reserve = max(MIN_STORAGE_RESERVE, min(MAX_STORAGE_RESERVE, usage.total // 20))
        if device not in volumes:
            volumes[device] = dict(path=str(path), requiredBytes=0, reserveBytes=reserve,
                                   freeBytes=usage.free, totalBytes=usage.total, purposes=[])
        volume = volumes[device]
        volume['requiredBytes'] += max(0, int(additional))
        volume['freeBytes'] = min(volume['freeBytes'], usage.free)
        volume['reserveBytes'] = max(volume['reserveBytes'], reserve)
        volume['purposes'].append(purpose)
    for volume in volumes.values():
        needed = volume['requiredBytes'] + volume['reserveBytes']
        if volume['freeBytes'] < needed:
            gib = 1024**3
            raise Problem(f'Not enough free disk space at {volume["path"]}: '
                          f'{needed/gib:.2f} GiB needed including a {volume["reserveBytes"]/gib:.2f} GiB safety reserve; '
                          f'{volume["freeBytes"]/gib:.2f} GiB available. '
                          'Free space or choose another destination. Existing backups are unchanged; Unforge does not automatically prune them.')
    return list(volumes.values())


def estimated_workspace_storage(listing):
    # Small files consume allocation blocks as well as plaintext bytes. The
    # allowance also covers the per-file recovery manifest and directory entries.
    source_bytes = sum(info[2] for info in listing.values())
    if source_bytes > MAX_BYTES:
        raise Problem('Workspace exceeds this backup limit (50 GiB). No source copy was started.')
    return sum(max(4096, ((info[2] + 4095) // 4096) * 4096) + 512 for info in listing.values()) + STORAGE_METADATA_ALLOWANCE


def tree_storage(path):
    size = 0
    for parent, dirs, files in os.walk(path, followlinks=False):
        size += 4096 * (1 + len(dirs))
        for name in files:
            source = Path(parent) / name
            if source.is_symlink():
                raise Problem('Storage estimation does not follow backup links.')
            info = source.stat()
            if not stat.S_ISREG(info.st_mode):
                raise Problem('Storage estimation requires ordinary backup files.')
            size += max(4096, ((info.st_size + 4095) // 4096) * 4096)
    return size


def atomic(path, value):
    path = Path(path)
    if path.is_symlink():
        raise Problem('Backup metadata must not be a symbolic link.')
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=True, indent=2)
            out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(name): os.unlink(name)


@contextmanager
def regular_reader(path):
    """Pin ancestors while opening; reject links and devices, including races."""
    path = Path(path).absolute()
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory); directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except OSError as error:
        raise Problem('Cannot safely read backup file: ' + str(path)) from error
    finally:
        os.close(directory)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise Problem('Backup contains a non-regular file: ' + str(path))
        yield stream


def digest(path, cancel=None):
    h = hashlib.sha256()
    with regular_reader(path) as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            if cancel is not None and cancel.is_set(): raise Problem('Backup verification cancelled.')
            h.update(chunk)
    return h.hexdigest()


def real_directory(path):
    path = Path(path)
    if path.is_symlink() or path.resolve() != path or not path.is_dir():
        raise Problem('Backup folder or an ancestor changed into a symbolic link: ' + str(path))


def sync_tree(path):
    """Flush every promoted byte and directory before reporting durable completion."""
    path = Path(path)
    real_directory(path)
    directories = []
    for parent, dirs, files in os.walk(path, followlinks=False):
        directories.append(Path(parent))
        for name in dirs:
            if (Path(parent) / name).is_symlink(): raise Problem('Backup directory links are not allowed.')
        for name in files:
            with regular_reader(Path(parent) / name) as reader: os.fsync(reader.fileno())
    for folder in reversed(directories):
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def restic_path():
    candidates = [Path(__file__).parent / '.tools/restic', Path(sys.executable).parent / 'restic',
                  Path(sys.executable).parent.parent / 'restic']
    executable = next((str(p) for p in candidates if p.is_file()), None)
    return executable or shutil.which('restic')


def cloud_helper(name):
    candidates = [Path(__file__).parent / ('.tools/' + name), Path(__file__).parent / name,
                  Path(sys.executable).parent / name, Path(sys.executable).parent.parent / name]
    return next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


class Backups:
    def __init__(self, engine, executable=None):
        self.engine = engine
        self.root = engine.home / '.backups'
        if self.root.is_symlink(): raise Problem('Backup state must not be a symbolic link.')
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.config_path = self.root / 'settings.json'
        self.key_path = self.root / 'recovery-key'
        self.lock = threading.RLock()
        self.executable = executable or restic_path()
        self.jobs = {}
        self.history_errors = []
        for file in self.root.glob('job-*.json'):
            try:
                with regular_reader(file) as reader:
                    raw = reader.read(4 * 1024**2 + 1)
                if len(raw) > 4 * 1024**2: raise Problem('Receipt exceeds its size limit.')
                job = json.loads(raw)
                if (not isinstance(job, dict) or not isinstance(job.get('id'), str)
                        or not re.fullmatch(r'[a-f0-9]{32}', job['id'])
                        or file.name != 'job-' + job['id'] + '.json'
                        or not isinstance(job.get('state'), str)
                        or job['state'] not in ('running', 'completed', 'failed', 'interrupted')
                        or not isinstance(job.get('createdAt', ''), str)
                        or not isinstance(job.get('destinations', []), list)):
                    raise Problem('Invalid backup receipt structure.')
                for field in ('phase', 'error', 'finishedAt', 'kind'):
                    if field in job and not isinstance(job[field], str): raise Problem('Invalid backup receipt field.')
                for destination in job.get('destinations', []):
                    if (not isinstance(destination, dict)
                            or destination.get('kind') not in ('icloud', 'drive', 'folder')
                            or not isinstance(destination.get('path'), str)):
                        raise Problem('Invalid backup receipt destination.')
                    for field in ('name', 'backupPath', 'vaultPath', 'state', 'checkedAt'):
                        if field in destination and not isinstance(destination[field], str): raise Problem('Invalid backup destination field.')
                # Older valid receipts may omit optional fields. Do not discard
                # their recovery paths simply because their metadata is sparse.
                job.setdefault('createdAt', '')
                job.setdefault('destinations', [])
                if job['state'] == 'running':
                    job.update(state='interrupted', error='Unforge stopped before this operation completed. No completed recovery point was replaced.')
                    try: atomic(file, job)
                    except OSError:
                        self.history_errors.append('An interrupted backup receipt could not be updated on disk. Its operation will not be restarted.')
                self.jobs[job['id']] = job
            except (ValueError, TypeError, KeyError, OSError, RecursionError):
                self.history_errors.append('A saved backup receipt could not be read. Its original file was preserved; other projects and recovery points remain available.')
        self.worker = None
        self.closed = threading.Event()
        self.children = set()
        self.children_lock = threading.Lock()

    def settings(self):
        if not self.config_path.exists() and not self.config_path.is_symlink():
            if next(self.root.glob('settings.invalid-*.json'), None) is not None:
                raise Problem('A backup settings repair did not finish. Save a destination again; the original settings remain archived.')
            return {'destinations': [], 'configured': False}
        try:
            with regular_reader(self.config_path) as reader:
                raw = reader.read(65537)
            if len(raw) > 65536: raise Problem('Backup settings exceed their supported size.')
            value = json.loads(raw)
            if (not isinstance(value, dict) or type(value.get('configured')) is not bool
                    or not isinstance(value.get('destinations'), list)
                    or not 0 <= len(value['destinations']) <= 4
                    or (value['configured'] and not value['destinations'])):
                raise Problem('Invalid backup destination settings.')
            paths = set()
            for item in value['destinations']:
                if (not isinstance(item, dict) or item.get('kind') not in ('icloud', 'drive', 'folder')
                        or not isinstance(item.get('path'), str) or not Path(item['path']).is_absolute()
                        or item['path'] in paths
                        or ('name' in item and not isinstance(item['name'], str))):
                    raise Problem('Invalid saved backup destination.')
                paths.add(item['path'])
            for field in ('updatedAt', 'encryption'):
                if field in value and not isinstance(value[field], str): raise Problem('Invalid backup setting.')
            if 'automatic' in value and type(value['automatic']) is not bool: raise Problem('Invalid backup preference.')
            return {key: value[key] for key in ('configured', 'destinations', 'updatedAt', 'encryption', 'automatic') if key in value}
        except (ValueError, TypeError, OSError, RecursionError) as error:
            raise Problem('Backup destination settings cannot be read. Save a destination to repair them. The original settings and existing recovery key will be preserved.') from error

    def state(self):
        try:
            value = {**self.settings(), 'settingsRecoveryRequired': False, 'settingsError': None}
        except (ValueError, TypeError, OSError):
            value = {'configured': False, 'destinations': [], 'settingsRecoveryRequired': True,
                     'settingsError': 'Backup destination settings need repair. Save a destination again to resume backups. Existing copies and the original settings are preserved; your recovery key is not reset.'}
        key_configured = False
        try:
            with regular_reader(self.key_path) as reader:
                key = reader.read(4097)
            key_configured = 12 <= len(key.decode('utf-8')) <= 1024
        except (ValueError, OSError): pass
        icloud = Path.home() / 'Library/Mobile Documents/com~apple~CloudDocs'
        suggestions = []
        if icloud.is_dir(): suggestions.append({'kind':'icloud', 'name':'iCloud Drive', 'path':str(icloud / 'Unforge Backups')})
        cloud = Path.home() / 'Library/CloudStorage'
        if cloud.is_dir():
            for path in cloud.iterdir():
                if path.name.startswith('GoogleDrive-') and (path / 'My Drive').is_dir():
                    suggestions.append({'kind':'drive', 'name':'Google Drive', 'path':str(path / 'My Drive/Unforge Backups')})
        return {**value, 'available':bool(self.executable), 'suggestions':suggestions,
                'cloudRehearsalAvailable':bool(cloud_helper('unforge-cloud-rehydrate')),
                'jobs':sorted(self.jobs.values(), key=lambda x:x.get('createdAt', ''), reverse=True)[:100],
                'historyErrors':list(self.history_errors), 'keyConfigured':key_configured,
                'scope':'Managed workspace source, local data, saved drafts and proposals. External databases, original folders and external credentials require their own backup bindings.',
                'cloudNotice':'A sync-folder copy remains upload pending until cloud receipt is verified. Keep your recovery key somewhere you can reach without this Mac.'}

    def configure(self, destinations, password=None):
        with self.lock:
            if self.worker and self.worker.is_alive(): raise Problem('Wait for the current backup operation.')
            if not self.executable: raise Problem('The restic backup tool is missing from this installation.')
            if not isinstance(destinations, list) or not 1 <= len(destinations) <= 4: raise Problem('Choose one to four backup destinations.')
            cleaned = []
            for item in destinations:
                if not isinstance(item, dict) or item.get('kind') not in ('icloud','drive','folder'): raise Problem('Choose iCloud Drive, Google Drive, or another folder.')
                if not isinstance(item.get('path'), str): raise Problem('Choose an absolute destination folder path.')
                path = Path(item['path']).expanduser()
                if not path.is_absolute(): raise Problem('Choose an absolute destination folder path.')
                if path.is_symlink(): raise Problem('Choose the destination itself, not a symbolic link.')
                target = path.resolve()
                if target == self.engine.home or target.is_relative_to(self.engine.home) or self.engine.home.is_relative_to(target):
                    raise Problem('Backup storage must be outside the workspace and cannot contain it.')
                if any(d['path'] == str(target) for d in cleaned): raise Problem('Choose distinct destinations.')
                cleaned.append({'kind':item['kind'], 'path':str(target), 'name':str(item.get('name') or item['kind'])[:100]})
            if self.key_path.is_symlink(): raise Problem('The saved recovery key is not a regular private file. Restore the original key before changing backup settings.')
            if not self.key_path.exists():
                if not isinstance(password,str) or not 12 <= len(password) <= 1024: raise Problem('Choose a recovery passphrase of at least 12 characters and keep it outside this Mac.')
                fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd,'w') as f: f.write(password); f.flush(); os.fsync(f.fileno())
            else:
                with regular_reader(self.key_path) as reader: saved_key = reader.read(4097)
                try: saved_key = saved_key.decode('utf-8')
                except UnicodeError as error: raise Problem('The saved recovery key cannot be read. Keep your original recovery passphrase; it has not been replaced.') from error
                if not 12 <= len(saved_key) <= 1024: raise Problem('The saved recovery key is damaged. It has not been replaced.')
                if password is not None and password != saved_key: raise Problem('Changing the recovery key would strand older backups. Use your existing key.')
            for d in cleaned:
                Path(d['path']).mkdir(parents=True, exist_ok=True)
                real_directory(Path(d['path']))
            result = {'configured':True, 'destinations':cleaned, 'updatedAt':stamp(), 'encryption':'restic repository v2', 'automatic':False}
            repair = False
            try: self.settings()
            except (ValueError, TypeError, OSError): repair = True
            if repair and (self.config_path.exists() or self.config_path.is_symlink()):
                if self.config_path.is_dir() and not self.config_path.is_symlink():
                    raise Problem('The backup settings path is a folder. Move it aside before repairing settings; its contents have not been changed.')
                archive = self.root / ('settings.invalid-' + uuid.uuid4().hex + '.json')
                os.rename(self.config_path, archive)
                sync_directory(self.root)
            atomic(self.config_path,result)
            return self.state()

    def _command(self, repository, key, *args, cwd=None):
        if not self.executable: raise Problem('Install the bundled restic backup tool.')
        env = {k:os.environ[k] for k in ('PATH','TMPDIR','SYSTEMROOT') if k in os.environ}
        env.update(HOME=str(self.root), RESTIC_PASSWORD_FILE=str(key), GOMAXPROCS='2')
        if self.closed.is_set(): raise Problem('Unforge is closing; the incomplete backup will not be promoted.')
        with self.children_lock:
            if self.closed.is_set(): raise Problem('Unforge is closing; backup cancelled.')
            process = subprocess.Popen([self.executable,'--repo',str(repository),'--no-cache','--json',*args],
                                    cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            self.children.add(process)
        try:
            stdout, stderr = process.communicate(timeout=1800)
        except BaseException:
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            process.communicate()
            raise
        finally:
            with self.children_lock: self.children.discard(process)
        if process.returncode: raise Problem('Backup tool failed: ' + stderr.decode(errors='replace')[-2000:])
        return stdout.decode(errors='replace')

    def _record(self, job):
        atomic(self.root / ('job-'+job['id']+'.json'), job)
        self.jobs[job['id']] = copy.deepcopy(job)

    def start(self, *, standalone=False):
        with self.lock:
            if not self.settings().get('configured'): raise Problem('Set up a backup destination and recovery key first.')
            if self.worker and self.worker.is_alive(): raise Problem('A backup or restore is already running.')
            job = {'id':uuid.uuid4().hex, 'kind':'backup', 'state':'running','createdAt':stamp(), 'phase':'Preparing a consistent copy', 'destinations':[]}
            self._record(job)
            self.worker = threading.Thread(target=self._backup if standalone else self._backup_incremental, args=(job,), daemon=False)
            self.worker.start()
            return dict(job)

    def _backup_incremental(self, job):
        try:
            from incremental_backups import IncrementalBackups
            IncrementalBackups(self).backup(job)
        except Exception as error:
            job.update(state='failed', error=str(error), finishedAt=stamp())
            self._record(job)

    def _workspace_listing(self):
        listing = {}
        for current, dirs, files in os.walk(self.engine.home, followlinks=False):
            real_directory(Path(current))
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not (Path(current)/d).is_symlink())
            for name in sorted(files):
                source = Path(current)/name
                if Path(current) == self.engine.home and name == 'UNFORGE-RECOVERY.json': continue
                if name in EXCLUDED or source.is_symlink(): continue
                info = source.stat()
                if not stat.S_ISREG(info.st_mode): continue
                listing[str(source.relative_to(self.engine.home))] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                if len(listing) > MAX_FILES: raise Problem('Workspace has too many files to snapshot.')
        return listing

    def _copy_workspace(self, target, capacity_check=None):
        """Copy files without following links; SQLite uses its online backup API."""
        manifest = {'schema':1,'createdAt':stamp(),'files':[], 'excluded':[], 'externalData':'Not included. Bind and export hosted data separately.'}
        baseline = self._workspace_listing()
        if capacity_check is not None:
            capacity_check(baseline)
        else:
            storage_guard([(target, estimated_workspace_storage(baseline), 'workspace snapshot')])
        sqlite_files, journal_files = set(), set()
        count = total = 0
        for current, dirs, files in os.walk(self.engine.home, followlinks=False):
            if self.closed.is_set(): raise Problem('Backup cancelled while copying source.')
            real_directory(Path(current))
            relative = Path(current).relative_to(self.engine.home)
            # Only journals belonging to a positively identified SQLite database
            # are omitted. A user file named notes-wal remains ordinary data.
            databases = set()
            for filename in files:
                candidate = Path(current)/filename
                if relative == Path('.') and filename == 'UNFORGE-RECOVERY.json': continue
                if filename in EXCLUDED or candidate.is_symlink() or not candidate.is_file(): continue
                with regular_reader(candidate) as reader:
                    if reader.read(16) == b'SQLite format 3\x00': databases.add(filename)
            journals = {filename + suffix for filename in databases for suffix in ('-wal','-shm','-journal')}
            journal_files.update(str(relative/name) for name in journals)
            for d in list(dirs):
                p = Path(current)/d
                if d in EXCLUDED or p.is_symlink():
                    manifest['excluded'].append({'path':str(relative/d),'reason':'rebuildable or operational cache' if d in EXCLUDED else 'symbolic link target not followed'})
                    dirs.remove(d)
            (target/relative).mkdir(parents=True,exist_ok=True)
            for name in files:
                source = Path(current)/name
                rel = relative/name
                if relative == Path('.') and name == 'UNFORGE-RECOVERY.json':
                    manifest['excluded'].append({'path':str(rel),'reason':'previous recovery inventory'})
                    continue
                if name in EXCLUDED or name in journals or source.is_symlink():
                    manifest['excluded'].append({'path':str(rel),'reason':'cache, SQLite journal, or symbolic link'})
                    continue
                before = source.stat()
                if not stat.S_ISREG(before.st_mode):
                    manifest['excluded'].append({'path':str(rel),'reason':'not a regular file'}); continue
                count += 1; total += before.st_size
                if count > MAX_FILES or total > MAX_BYTES: raise Problem('Workspace exceeds this backup limit (250,000 files / 50 GiB). No complete-backup claim was recorded.')
                destination = target/rel
                sqlite = name in databases
                if sqlite:
                    sqlite_files.add(str(rel))
                    from urllib.parse import quote
                    # SQLite's connection context manager does not close the
                    # connection. Close both explicitly, and make the snapshot
                    # independent of WAL/SHM sidecars before hashing it.
                    with closing(sqlite3.connect('file:'+quote(str(source))+'?mode=ro',uri=True,timeout=10)) as db:
                        with closing(sqlite3.connect(destination)) as out:
                            started = time.monotonic()
                            page_size = db.execute('PRAGMA page_size').fetchone()[0]
                            def progress(status, remaining, page_count):
                                if self.closed.is_set() or time.monotonic() - started > 30:
                                    raise Problem('SQLite snapshot timed out or was cancelled: ' + str(rel))
                                storage_guard([(target, max(remaining, 1) * page_size, 'SQLite snapshot')])
                            db.backup(out, pages=256, progress=progress, sleep=0.05)
                            checkpoint = out.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                            if checkpoint[0] != 0:
                                raise Problem('SQLite snapshot could not finish its checkpoint: ' + str(rel))
                            if out.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower() != 'delete':
                                raise Problem('SQLite snapshot could not become self-contained: ' + str(rel))
                            if out.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise Problem('SQLite integrity check failed: '+str(rel))
                    if any(Path(str(destination) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
                        raise Problem('SQLite snapshot still depends on a journal: ' + str(rel))
                    destination.chmod(stat.S_IMODE(before.st_mode))
                    with regular_reader(destination) as reader: os.fsync(reader.fileno())
                    sync_directory(destination.parent)
                else:
                    with regular_reader(source) as reader, destination.open('wb') as writer:
                        copied = 0
                        while True:
                            if self.closed.is_set(): raise Problem('Backup cancelled while copying source.')
                            chunk = reader.read(1024 * 1024)
                            if not chunk: break
                            copied += len(chunk)
                            if copied > before.st_size: raise Problem('File grew during backup: ' + str(rel))
                            writer.write(chunk)
                        writer.flush(); os.fsync(writer.fileno())
                    destination.chmod(stat.S_IMODE(before.st_mode))
                    after = source.stat()
                    if (before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_ino,after.st_size,after.st_mtime_ns):
                        raise Problem('A file changed during backup. Retry after saving: '+str(rel))
                manifest['files'].append({'path':str(rel),'bytes':destination.stat().st_size,'sha256':digest(destination,self.closed),'sqlite':sqlite})
        after = self._workspace_listing()
        ignored = sqlite_files | journal_files
        if {key:value for key,value in baseline.items() if key not in ignored} != {key:value for key,value in after.items() if key not in ignored}:
            raise Problem('Workspace files changed during the snapshot. Save external edits and retry.')
        manifest.update(fileCount=count,sourceBytes=total, consistency='Managed engine writes were paused; ordinary files were checked before and after capture. Each SQLite database was backed up transactionally, independently. External applications were not quiesced; this is not an atomic multi-database snapshot.')
        atomic(target/'UNFORGE-RECOVERY.json',manifest)
        return manifest

    def _backup(self, job):
        try:
            destinations = self.settings()['destinations']
            for item in destinations: real_directory(Path(item['path']))
            estimates = {}
            def capacity_check(listing):
                source = estimated_workspace_storage(listing)
                # Independent encrypted repositories cannot rely on deduplication
                # against previous recovery points; budget for incompressible data.
                repository = (source * 115 + 99) // 100 + STORAGE_METADATA_ALLOWANCE
                estimates.update(source=source, repository=repository)
                job['storagePlan'] = storage_guard([(self.root, 2 * source + repository, 'source, encryption, and verification')]
                    + [(Path(item['path']), repository + STORAGE_METADATA_ALLOWANCE, 'completed recovery point') for item in destinations])
            with self.engine.lock: capacity_check(self._workspace_listing())
            with tempfile.TemporaryDirectory(prefix='snapshot-',dir=self.root) as tmp:
                temp=Path(tmp); workspace=temp/'workspace'; workspace.mkdir()
                with self.engine.lock: manifest=self._copy_workspace(workspace, capacity_check=capacity_check)
                estimates['source'] = tree_storage(workspace) + STORAGE_METADATA_ALLOWANCE
                estimates['repository'] = (estimates['source'] * 115 + 99) // 100 + STORAGE_METADATA_ALLOWANCE
                repository=temp/'repository'
                storage_guard([(temp, estimates['repository'] + estimates['source'], 'encryption and verification')]
                    + [(Path(item['path']), estimates['repository'] + STORAGE_METADATA_ALLOWANCE, 'completed recovery point') for item in destinations])
                self._command(repository,self.key_path,'init','--repository-version','2')
                self._command(repository,self.key_path,'backup','--host','unforge','--tag',job['id'],'workspace',cwd=temp)
                job['phase']='Checking encrypted recovery point'; self._record(job)
                self._command(repository,self.key_path,'check','--read-data')
                # A separate restore proves decryptability and every retained file digest.
                restored=temp/'verification'
                repository_bytes = tree_storage(repository) + STORAGE_METADATA_ALLOWANCE
                storage_guard([(temp, estimates['source'], 'verification restore')]
                    + [(Path(item['path']), repository_bytes, 'completed recovery point') for item in destinations])
                self._command(repository,self.key_path,'restore','latest','--target',str(restored))
                self.verify_workspace(restored/'workspace',self.closed)
                for index, d in enumerate(destinations):
                    dest=Path(d['path']); real_directory(dest); name='unforge-'+job['createdAt'][:10]+'-'+job['id']+'.ufbackup'
                    storage_guard([(Path(item['path']), repository_bytes, 'recovery point copy') for item in destinations[index:]])
                    staging=dest/('.pending-'+job['id']); final=dest/name
                    if staging.exists() or final.exists(): raise Problem('A backup with this identifier already exists.')
                    staging.mkdir(mode=0o700)
                    try:
                        shutil.copytree(repository,staging/'repository')
                        (staging/'RESTORE.txt').write_text('Unforge encrypted recovery point\nUse Unforge Recover with your recovery passphrase, or standard restic:\nrestic -r repository restore latest --target recovered\nThe passphrase is not in this backup.\nOpen recovered/workspace/UNFORGE-RECOVERY.json to see scope and exclusions.\nNever start restored scheduled jobs until destinations are reviewed.\n')
                        files=[{'path':str(p.relative_to(staging)),'sha256':digest(p),'bytes':p.stat().st_size} for p in staging.rglob('*') if p.is_file()]
                        atomic(staging/'CONTENTS.json',{'schema':1,'files':files})
                        self.verify_container(staging,self.closed)
                        sync_tree(staging)
                        real_directory(dest)
                        os.replace(staging,final)
                        sync_directory(dest)
                        self.verify_container(final,self.closed)
                    except Exception:
                        # A partial upload is never promoted or presented as a recovery point.
                        if staging.exists(): shutil.rmtree(staging)
                        raise
                    job['destinations'].append({**d,'backupPath':str(final),'state':'upload_pending' if d['kind'] in ('icloud','drive') else 'copied_locally','readbackAt':stamp()})
                    self._record(job)
                job.update(state='completed',phase='Encrypted copy verified locally',finishedAt=stamp(),files=manifest['fileCount'],bytes=manifest['sourceBytes'],excludedCount=len(manifest['excluded']),restoredLocally=True)
        except Exception as error:
            job.update(state='failed',error=str(error),finishedAt=stamp())
        self._record(job)

    @staticmethod
    def _verify_manifest(path, marker_name, cancel=None):
        path = Path(path)
        real_directory(path)
        marker = path / marker_name
        with regular_reader(marker) as reader:
            raw = reader.read(64 * 1024 * 1024 + 1)
        if len(raw) > 64 * 1024 * 1024: raise Problem('Backup inventory exceeds its size limit.')
        try: manifest = json.loads(raw)
        except (ValueError, UnicodeError) as error: raise Problem('Invalid backup inventory.') from error
        if not isinstance(manifest, dict) or manifest.get('schema') != 1 or not isinstance(manifest.get('files'), list) or len(manifest['files']) > MAX_FILES:
            raise Problem('Invalid backup inventory.')
        expected, total = set(), 0
        for entry in manifest['files']:
            if cancel is not None and cancel.is_set(): raise Problem('Backup verification cancelled.')
            if not isinstance(entry, dict): raise Problem('Invalid backup inventory entry.')
            name, size, checksum = entry.get('path'), entry.get('bytes'), entry.get('sha256')
            if not isinstance(name, str) or not name or Path(name).is_absolute() or any(part in ('', '.', '..') for part in name.split('/')) or '\\' in name or '\0' in name or name == marker_name:
                raise Problem('Invalid backup path.')
            if name in expected or isinstance(size, bool) or not isinstance(size, int) or size < 0 or not isinstance(checksum, str) or not re.fullmatch(r'[a-f0-9]{64}', checksum):
                raise Problem('Invalid or duplicate backup inventory entry.')
            total += size
            if total > MAX_BYTES: raise Problem('Backup exceeds the supported size limit.')
            candidate = path / name
            with regular_reader(candidate) as reader:
                if os.fstat(reader.fileno()).st_size != size: raise Problem('Backup is incomplete or damaged: ' + name)
            if digest(candidate,cancel) != checksum: raise Problem('Backup is incomplete or damaged: ' + name)
            expected.add(name)
        actual = set()
        for parent, dirs, files in os.walk(path, followlinks=False):
            for name in dirs:
                if (Path(parent)/name).is_symlink(): raise Problem('Backup directory links are not allowed.')
            for name in files:
                candidate = Path(parent)/name
                if candidate == marker: continue
                with regular_reader(candidate): pass
                actual.add(candidate.relative_to(path).as_posix())
                if len(actual) > MAX_FILES: raise Problem('Backup contains too many files.')
        if actual != expected: raise Problem('Unexpected or missing files in recovery point.')
        return manifest

    @staticmethod
    def verify_container(path, cancel=None):
        return Backups._verify_manifest(path, 'CONTENTS.json', cancel)

    @staticmethod
    def verify_workspace(path, cancel=None):
        manifest = Backups._verify_manifest(path, 'UNFORGE-RECOVERY.json', cancel)
        if manifest.get('fileCount') != len(manifest['files']): raise Problem('Recovery file count does not match its inventory.')
        return manifest

    def restore(self, path, password, destination):
        if isinstance(path, str) and Path(path).suffix == '.ufpoint':
            from incremental_backups import IncrementalBackups
            return IncrementalBackups(self).restore(path, password, destination)
        with self.lock:
            if self.worker and self.worker.is_alive(): raise Problem('Wait for the current backup operation.')
            source=Path(path).expanduser()
            target=Path(destination).expanduser()
            if not source.is_absolute() or not target.is_absolute(): raise Problem('Choose absolute backup and recovery paths.')
            if target.exists() or target.is_symlink(): raise Problem('Restore into a new folder; existing work is never overwritten.')
            if target.resolve().is_relative_to(self.engine.home) or self.engine.home.is_relative_to(target.resolve()): raise Problem('Recover outside your active workspace.')
            if not isinstance(password,str) or not password: raise Problem('Enter your recovery passphrase.')
            if source.is_symlink(): raise Problem('Backup links are not allowed.')
            source=source.resolve()
            target=target.resolve()
            self.verify_container(source,self.closed)
            target.parent.mkdir(parents=True,exist_ok=True)
            real_directory(target.parent)
            with tempfile.TemporaryDirectory(prefix='.unforge-restore-',dir=target.parent) as tmp:
                temp=Path(tmp); key=temp/'key'; key.write_text(password); key.chmod(0o600)
                statistics = json.loads(self._command(source/'repository', key, 'stats', 'latest', '--mode', 'restore-size'))
                size, count = statistics.get('total_size'), statistics.get('total_file_count')
                # Restic's totals also include the recovery manifest and directory
                # nodes, beyond the source-byte/file limits in that manifest.
                if type(size) is not int or type(count) is not int or size < 0 or count < 0 or size > MAX_BYTES + MAX_FILES * 4096 + STORAGE_METADATA_ALLOWANCE or count > MAX_FILES * 4 + 1:
                    raise Problem('The encrypted recovery size is unavailable or exceeds the supported restore limit.')
                storage_guard([(target.parent, size + count * 4096 + STORAGE_METADATA_ALLOWANCE, 'workspace recovery')])
                self._command(source/'repository',key,'restore','latest','--target',str(temp/'result'))
                manifest=self.verify_workspace(temp/'result/workspace',self.closed)
                # Nothing is executed. Runtime bindings are preserved as evidence,
                # but the caller chooses when/how to activate the recovered workspace.
                sync_tree(temp/'result/workspace')
                real_directory(target.parent)
                if target.exists() or target.is_symlink(): raise Problem('Recovery destination was created by another operation.')
                os.rename(temp/'result/workspace',target)
                sync_directory(target.parent)
            result={'path':str(target),'files':manifest['fileCount'],'restoredAt':stamp(),'state':'restored','applicationsStarted':False,'externalData':manifest['externalData'],'excluded':manifest['excluded']}
            atomic(self.root/('restore-'+uuid.uuid4().hex+'.json'),result)
            return result

    def close(self):
        self.closed.set()
        with self.children_lock: children = list(self.children)
        for process in children:
            try: os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError: pass
        if self.worker and self.worker.is_alive(): self.worker.join(timeout=5)
        with self.children_lock: children = list(self.children)
        for process in children:
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
        if self.worker and self.worker.is_alive(): self.worker.join()

    def _cloud_report(self, helper, path, timeout=15):
        environment = {key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG','LC_ALL') if key in os.environ}
        with self.children_lock:
            if self.closed.is_set(): raise Problem('Unforge is closing.')
            process = subprocess.Popen([str(helper), str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, start_new_session=True, env=environment)
            self.children.add(process)
        output = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + timeout
                while selector.get_map():
                    if self.closed.is_set() or time.monotonic() >= deadline:
                        raise Problem('iCloud operation did not finish. No complete recovery proof was recorded.')
                    for key, _ in selector.select(min(0.2, max(0, deadline-time.monotonic()))):
                        chunk = os.read(key.fd, 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(chunk)
                            if len(output) > 65536: raise Problem('iCloud metadata response exceeded its limit.')
                process.wait(timeout=max(0.01, deadline-time.monotonic()))
                if process.returncode: raise Problem('Could not read iCloud upload state.')
                return json.loads(output)
        finally:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait()
            process.stdin.close()
            process.stdout.close()
            with self.children_lock: self.children.discard(process)

    def cloud_rehearse(self, job_id, path, password, destination):
        """Evict only a registered iCloud backup's cache, then prove recovery."""
        with self.lock:
            if self.worker and self.worker.is_alive(): raise Problem('Wait for the current backup operation.')
            if not isinstance(job_id, str) or job_id not in self.jobs:
                raise Problem('Choose a known recovery point.')
            job = copy.deepcopy(self.jobs[job_id])
            if job.get('state') != 'completed': raise Problem('Choose a completed recovery point.')
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise Problem('Choose the registered iCloud recovery point.')
            source = Path(path)
            real_directory(source)
            chosen = next((item for item in job.get('destinations', [])
                           if item.get('kind') == 'icloud' and item.get('backupPath') == str(source)), None)
            registered = {(item.get('kind'), item.get('path')) for item in self.settings().get('destinations', [])}
            if chosen is None or ('icloud', chosen.get('path')) not in registered:
                raise Problem('Only a completed recovery point in your registered iCloud backup folder can be rehearsed.')
            incremental = chosen.get('format') == 'incremental-v2'
            inspection = None
            if incremental:
                from incremental_backups import IncrementalBackups
                reader = IncrementalBackups(self)
                inspection = reader.inspect(source)
                cloud_path = Path(inspection['vaultPath'])
                if (chosen.get('vaultPath') != str(cloud_path) or cloud_path.parent != Path(chosen['path'])
                        or inspection['pointId'] != job_id or inspection['snapshotId'] != job.get('snapshotId')
                        or inspection['repositoryId'] != job.get('repositoryId')):
                    raise Problem('The recovery point does not match its registered shared vault and exact snapshot.')
                repository = Path(inspection['repositoryPath'])
                snapshot = inspection['snapshotId']
            else:
                if source.parent != Path(chosen['path']) or source.suffix != '.ufbackup':
                    raise Problem('Choose a registered standalone recovery point or incremental shared vault point.')
                cloud_path, repository, snapshot = source, source / 'repository', 'latest'

            def vault_inventory():
                # The selected point can be older than the vault's newest point.
                # Hash every file, including later packs and descriptors, so the
                # helper's counts and rehydration claim cover its actual target.
                inventory, total, entries = {}, 0, 0
                def failed_walk(error): raise Problem('The shared vault inventory could not be read.') from error
                for parent, directories, names in os.walk(cloud_path, followlinks=False, onerror=failed_walk):
                    real_directory(Path(parent))
                    entries += len(directories) + len(names)
                    if entries > 20000: raise Problem('The shared vault exceeds the 20,000-entry cloud recovery limit.')
                    for name in directories:
                        if (Path(parent) / name).is_symlink(): raise Problem('Shared vault links cannot be rehydrated.')
                    for name in names:
                        candidate = Path(parent) / name
                        with regular_reader(candidate) as stream:
                            before = os.fstat(stream.fileno())
                        total += before.st_size
                        if len(inventory) >= 10000 or total > 1024**4:
                            raise Problem('The shared vault exceeds the supported cloud recovery inventory limit.')
                        checksum = digest(candidate, self.closed)
                        with regular_reader(candidate) as stream:
                            after = os.fstat(stream.fileno())
                        if (before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_ino,after.st_size,after.st_mtime_ns):
                            raise Problem('The shared vault changed while its cloud inventory was being read.')
                        inventory[candidate.relative_to(cloud_path).as_posix()] = (before.st_size,checksum)
                if not inventory: raise Problem('The shared vault contains no files.')
                return inventory
            if not isinstance(destination, str) or not Path(destination).expanduser().is_absolute():
                raise Problem('Choose an absolute new recovery folder.')
            target = Path(destination).expanduser()
            if target.exists() or target.is_symlink(): raise Problem('Recover into a new folder; existing work is never overwritten.')
            # Resolve existing ancestors before eviction, without silently accepting links.
            ancestor = target.parent
            while not ancestor.exists():
                if ancestor.is_symlink(): raise Problem('Recovery folders cannot contain symbolic links.')
                ancestor = ancestor.parent
            real_directory(ancestor)
            target = target.resolve()
            if target.is_relative_to(self.engine.home) or self.engine.home.is_relative_to(target):
                raise Problem('Recover outside your active workspace.')
            if target.is_relative_to(cloud_path) or cloud_path.is_relative_to(target):
                raise Problem('Recover outside the encrypted recovery point and its shared vault.')
            if not isinstance(password, str) or not 1 <= len(password) <= 1024:
                raise Problem('Enter your recovery passphrase before testing iCloud recovery.')
            helper = cloud_helper('unforge-cloud-rehydrate')
            if helper is None: raise Problem('This installation is missing its iCloud recovery helper.')
            inventory = vault_inventory() if incremental else self.verify_container(source, self.closed)
            evidence = dict(state='checking', startedAt=stamp(), contentVerified=False, secondDeviceVerified=False,
                            scope='entire shared vault and selected exact snapshot' if incremental else 'standalone recovery point',
                            cloudPath=str(cloud_path))
            chosen['cloudRecoveryEvidence'] = evidence
            self._record(job)
            try:
                # Verify the supplied key before requesting cache eviction. Keep
                # the key out of arguments, helper environment and receipts.
                with tempfile.TemporaryDirectory(prefix='cloud-key-', dir=self.root) as temporary:
                    key = Path(temporary) / 'key'
                    with key.open('x') as output:
                        key.chmod(0o600); output.write(password)
                    statistics = json.loads(self._command(repository, key, '--no-lock', 'stats', snapshot, '--mode', 'restore-size'))
                    size, count = statistics.get('total_size'), statistics.get('total_file_count')
                    if type(size) is not int or type(count) is not int or size < 0 or count < 0 or size > MAX_BYTES + MAX_FILES * 4096 + STORAGE_METADATA_ALLOWANCE or count > MAX_FILES * 4 + 1:
                        raise Problem('The encrypted recovery size is unavailable or exceeds the supported restore limit.')
                    storage_guard([(target.parent, size + count * 4096 + STORAGE_METADATA_ALLOWANCE, 'cloud recovery rehearsal')])
                report = self._cloud_report(helper, cloud_path, timeout=130)
                if (not isinstance(report, dict) or report.get('state') != 'rehydrated' or report.get('error')
                        or report.get('evictionVerified') is not True or report.get('contentVerified') is not False
                        or report.get('secondDeviceVerified') is not False):
                    raise Problem('iCloud did not provide complete eviction and download evidence. Recovery from the cloud remains unverified.')
                expected_files = len(inventory) if incremental else len(inventory['files']) + 1
                if any(type(report.get(field)) is not int or report[field] != expected_files
                       for field in ('files', 'uploaded', 'evicted', 'downloaded')):
                    raise Problem('The iCloud file counts do not match the recovery point inventory.')
                if incremental:
                    if vault_inventory() != inventory:
                        raise Problem('The downloaded shared vault differs from its complete pre-eviction inventory.')
                    if reader.inspect(source) != inspection:
                        raise Problem('The selected recovery point changed during cloud recovery.')
                evidence.update(rehydration=report, state='verifying')
                self._record(job)
                result = self.restore(str(source), password, str(target))
                evidence.update(state='verified', finishedAt=stamp(), contentVerified=True,
                                restoredPath=result['path'], files=result['files'],
                                vaultFilesVerified=expected_files if incremental else None,
                                snapshotId=snapshot if incremental else None,
                                evidence='This Mac evicted the encrypted local cache, observed iCloud downloading it again, and verified the decrypted restored files. Recovery on a second device has not been tested.')
                self._record(job)
                return {**result, 'cloudRecoveryEvidence':copy.deepcopy(evidence)}
            except Exception as error:
                evidence.update(state='failed', finishedAt=stamp(), contentVerified=False,
                                error=str(error)[:1000])
                self._record(job)
                raise

    def refresh_cloud(self, job_id):
        with self.lock:
            if self.worker and self.worker.is_alive(): raise Problem('Wait for the current backup operation.')
            if not isinstance(job_id,str) or job_id not in self.jobs: raise Problem('Choose a known recovery point.')
            job = copy.deepcopy(self.jobs[job_id])
            if job['state'] != 'completed': raise Problem('Wait for a completed recovery point.')
            helper = cloud_helper('unforge-cloud-status')
            for d in job.get('destinations',[]):
                if d['kind'] != 'icloud': continue
                d.update(state='upload_pending', checkedAt=stamp())
                if not helper:
                    d['uploadEvidence']='iCloud upload metadata helper unavailable in this installation.'
                    continue
                try:
                    cloud_path = d['backupPath']
                    expected_files = None
                    if d.get('format') == 'incremental-v2':
                        from incremental_backups import IncrementalBackups
                        inspection = IncrementalBackups(self).inspect(cloud_path)
                        vault = Path(inspection['vaultPath'])
                        registered = {(item.get('kind'),item.get('path')) for item in self.settings().get('destinations',[])}
                        if (('icloud',d.get('path')) not in registered or d.get('vaultPath') != str(vault)
                                or vault.parent != Path(d['path']) or inspection['pointId'] != job_id
                                or inspection['snapshotId'] != job.get('snapshotId')
                                or inspection['repositoryId'] != job.get('repositoryId')):
                            raise Problem('Recovery point does not match its registered shared vault.')
                        cloud_path = str(vault)
                        def metadata_inventory():
                            inventory, entries = {}, 0
                            def failed_walk(error): raise Problem('The shared vault inventory could not be read.') from error
                            for parent, directories, names in os.walk(vault,followlinks=False,onerror=failed_walk):
                                real_directory(Path(parent))
                                entries += len(directories) + len(names)
                                if entries > 10000: raise Problem('The shared vault exceeds the upload check inventory limit.')
                                for name in directories:
                                    if (Path(parent)/name).is_symlink(): raise Problem('Shared vault links cannot be inspected.')
                                for name in names:
                                    candidate = Path(parent)/name
                                    with regular_reader(candidate) as stream: info = os.fstat(stream.fileno())
                                    inventory[candidate.relative_to(vault).as_posix()] = (info.st_ino,info.st_size,info.st_mtime_ns)
                            return inventory
                        inventory = metadata_inventory()
                        expected_files = len(inventory)
                    report=self._cloud_report(helper,cloud_path)
                    if not isinstance(report,dict) or report.get('state') not in ('uploaded','upload_pending','not_icloud','unknown'):
                        raise Problem('Unrecognized iCloud metadata response.')
                    if report['state'] == 'uploaded':
                        count, uploaded = report.get('files'),report.get('uploaded')
                        if type(count) is not int or type(uploaded) is not int or count <= 0 or uploaded != count or report.get('error') or (expected_files is not None and count != expected_files):
                            raise Problem('Incomplete iCloud upload evidence.')
                        if expected_files is not None and metadata_inventory() != inventory:
                            raise Problem('The shared vault changed during its upload check.')
                        d['state']='cloud_uploaded'
                    if expected_files is not None:
                        report = {**report,'scope':'entire shared vault','vaultPath':cloud_path,'selectedSnapshotId':inspection['snapshotId']}
                    d['uploadEvidence']=report
                except (OSError,ValueError,subprocess.SubprocessError) as e:
                    d['uploadEvidence']='Upload status unavailable: '+str(e)
            self._record(job)
            return job
