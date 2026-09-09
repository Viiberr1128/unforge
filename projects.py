"""Read-only folder inventory, transactional adoption, and paginated source access."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid

from engine import MAX_TEXT, Problem

MAX_FILES = 10000
MAX_BYTES = 128 * 1024 * 1024
MAX_GIT_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 30000
SKIP_DIRS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', '.next', '.nuxt',
             'dist', 'build', 'coverage', '.cache', '.idea', '.DS_Store', 'data',
             'uploads', 'backups', '.recovery'}
DATA_SUFFIXES = ('.sqlite', '.sqlite3', '.db', '.db-wal', '.db-shm', '.sqlite-wal',
                 '.sqlite-shm', '.log')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _read_regular(path, maximum):
    """Do not follow a replaced final symlink; reject changes during capture."""
    try:
        # Open each ancestor relative to a pinned directory descriptor so a folder
        # swapped for a symlink cannot redirect an inventory into private data.
        absolute = Path(path).absolute()
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in absolute.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, 'rb') as reader:
            before = os.fstat(reader.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                raise Problem('File is not a supported regular file: ' + str(path))
            data = reader.read(maximum + 1)
            after = os.fstat(reader.fileno())
            if len(data) > maximum or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise Problem('File changed while being read: ' + str(path))
            return data, before.st_mode
    except OSError as error:
        raise Problem('Cannot safely read file: ' + str(path)) from error


class Projects:
    def __init__(self, engine):
        self.engine = engine

    def _source(self, path):
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise Problem('Choose an absolute local folder path')
        source = Path(path)
        if source.is_symlink() or not source.is_dir() or source.resolve() != source:
            raise Problem('Choose a real folder without symbolic links in its path')
        # A home import would ingest managed projects and transient operation state.
        if source == self.engine.home or source in self.engine.home.parents:
            raise Problem('Choose an application folder, not the Unforge data folder or its parent')
        return source

    def _git_layout(self, source):
        marker = source / '.git'
        if not marker.exists() and not marker.is_symlink():
            return dict(kind='folder', history='none'), None, None
        if marker.is_symlink():
            return dict(kind='git', history='unsupported', reason='The Git directory is a symbolic link'), None, None
        try:
            if marker.is_file():
                raw, _ = _read_regular(marker, 4096)
                value = raw.decode().strip()
                if not value.startswith('gitdir: '):
                    raise Problem('Invalid Git worktree pointer')
                gitdir = (source / value[8:]).resolve()
                kind = 'worktree'
            else:
                gitdir, kind = marker, 'git'
            if not gitdir.is_dir() or gitdir.is_symlink():
                raise Problem('Git storage is unavailable')
            common = gitdir
            if (gitdir / 'commondir').exists():
                raw, _ = _read_regular(gitdir / 'commondir', 4096)
                common = (gitdir / raw.decode().strip()).resolve()
            if (common / 'shallow').exists() or (common / 'objects' / 'info' / 'alternates').exists():
                raise Problem('Shallow or alternate-object repositories need a complete Git bundle')
            if not (common / 'objects').is_dir() or (common / 'objects').is_symlink():
                raise Problem('Git object storage is unavailable')
            head, _ = _read_regular(gitdir / 'HEAD', 4096)
            refs = []
            for base in ('refs',):
                folder = common / base
                if folder.is_symlink():
                    raise Problem('Linked Git refs are not supported')
                if folder.exists():
                    for parent, dirs, files in os.walk(folder, followlinks=False):
                        for name in dirs + files:
                            item = Path(parent) / name
                            if item.is_symlink():
                                raise Problem('Linked Git refs are not supported')
                        for name in files:
                            item = Path(parent) / name
                            data, _ = _read_regular(item, 4096)
                            refs.append((item.relative_to(common).as_posix(), data.decode()))
                            if len(refs) > MAX_FILES:
                                raise Problem('Too many Git refs')
            packed = ''
            if (common / 'packed-refs').exists():
                packed = _read_regular(common / 'packed-refs', 2 * 1024 * 1024)[0].decode()
            if any(name.startswith('refs/replace/') for name, _ in refs) or ' refs/replace/' in packed:
                raise Problem('Replacement refs need a separately reviewed Git bundle')
            return dict(kind=kind, history='preserved', head=head.decode().strip(),
                        refsRevision=_digest([head.decode(), sorted(refs), packed]),
                        note='All supported refs and reachable history are retained. Historical secrets are not scanned. Index staging, reflogs, hooks and local Git configuration are not copied.'), gitdir, common
        except (OSError, UnicodeError, Problem) as error:
            return dict(kind='git', history='unsupported', reason=str(error)), None, None

    def _scan(self, source, destination=None):
        included, skipped = [], []
        total = entries = 0
        for parent, dirs, files in os.walk(source, topdown=True, followlinks=False):
            dirs.sort()
            files.sort()
            for name in list(dirs):
                item = Path(parent) / name
                relative = item.relative_to(source).as_posix()
                entries += 1
                reason = None
                if item.is_symlink():
                    reason = 'Symbolic links are not imported'
                elif name in SKIP_DIRS:
                    reason = ('Git storage is handled separately' if Path(parent) == source else 'Nested Git history is not imported') if name == '.git' else 'Generated files or runtime data; not application source'
                else:
                    try:
                        self.engine.safe_path(source, relative)
                    except Problem as error:
                        reason = str(error)
                if reason:
                    dirs.remove(name)
                    skipped.append(dict(path=relative + '/', reason=reason))
            for name in files:
                entries += 1
                item = Path(parent) / name
                relative = item.relative_to(source).as_posix()
                reason = None
                if name == '.git':
                    reason = 'Git storage is handled separately' if Path(parent) == source else 'Nested Git history is not imported'
                elif name in SKIP_DIRS or name.lower().endswith(DATA_SUFFIXES):
                    reason = 'Generated files or runtime data; not application source'
                elif relative.lower() == '.unforge/project.json':
                    reason = 'Project identity is created for the independent copy'
                else:
                    try:
                        self.engine.safe_path(source, relative)
                    except Problem as error:
                        reason = str(error)
                if reason:
                    if name.lower().startswith('.env.') and name.lower().endswith(('example', 'sample', 'template')):
                        reason = 'This environment template is excluded by the current engine private-path rule; recreate its non-secret setup instructions after import'
                    skipped.append(dict(path=relative, reason=reason))
                    continue
                data, mode = _read_regular(item, MAX_BYTES)
                total += len(data)
                if total > MAX_BYTES or len(included) >= MAX_FILES:
                    raise Problem('Folder exceeds the 10,000-file or 128 MiB source limit; choose a smaller application folder')
                record = dict(path=relative, size=len(data), sha256=hashlib.sha256(data).hexdigest(), executable=bool(mode & 0o111))
                included.append(record)
                if destination is not None:
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    target.chmod(0o755 if record['executable'] else 0o644)
            if entries > MAX_ENTRIES:
                raise Problem('Folder contains too many entries; choose a smaller application folder')
        ignored = self._ignored(source, included)
        retained = []
        for record in included:
            if record['path'] in ignored:
                skipped.append(dict(path=record['path'], reason='Excluded by the project Git ignore rules'))
                total -= record['size']
                if destination is not None:
                    (destination / record['path']).unlink()
            else:
                retained.append(record)
        return sorted(retained, key=lambda x: x['path']), sorted(skipped, key=lambda x: x['path']), total

    def _ignored(self, source, records):
        # Evaluate ignore rules in a fresh repository: no source config or filters
        # can execute. Copying the index makes tracked-but-ignored files remain visible.
        with tempfile.TemporaryDirectory(prefix='unforge-ignore-') as temporary:
            probe = Path(temporary)
            self.engine.git(probe, 'init', '--template=', '-b', 'main')
            _, gitdir, common = self._git_layout(source)
            if gitdir is not None and (gitdir / 'index').exists():
                index, _ = _read_regular(gitdir / 'index', 16 * 1024 * 1024)
                (probe / '.git/index').write_bytes(index)
                # Split indexes refer to immutable shared index files by checksum.
                for shared in gitdir.glob('sharedindex.*'):
                    if not re.fullmatch(r'sharedindex\.[a-f0-9]{40,64}', shared.name):
                        raise Problem('Unsupported Git index storage')
                    data, _ = _read_regular(shared, 16 * 1024 * 1024)
                    (probe / '.git' / shared.name).write_bytes(data)
            if common is not None and (common / 'info/exclude').exists():
                data, _ = _read_regular(common / 'info/exclude', 1024 * 1024)
                (probe / '.git/info/exclude').write_bytes(data)
            for record in records:
                if Path(record['path']).name == '.gitignore':
                    target = probe / record['path']
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(_read_regular(source / record['path'], MAX_BYTES)[0])
            ignored = set()
            names = [record['path'] for record in records]
            for start in range(0, len(names), 200):
                raw = self.engine.git(probe, '-c', 'core.quotePath=false', 'check-ignore', '--', *names[start:start + 200], allowed_returncodes=(0, 1))
                ignored.update(json.loads(line) if line.startswith(chr(34)) else line for line in raw.decode().splitlines())
            return ignored

    def inventory(self, path):
        source = self._source(path)
        included, skipped, total = self._scan(source)
        git, _, _ = self._git_layout(source)
        omissions = [item for item in skipped if item['reason'] != 'Git storage is handled separately']
        result = dict(sourcePath=str(source), files=included, skipped=skipped, fileCount=len(included),
                      totalBytes=total, git=git, partial=bool(omissions or git['history'] == 'unsupported'),
                      mode='independent-copy',
                      limitations=['The original is never changed. Later edits do not synchronize automatically.',
                                   'Only listed source files are included; runtime data and credentials need separate setup.',
                                   'Project .gitignore and Git info/exclude rules are applied; user-global ignore configuration is not copied.',
                                   'Filename checks do not detect secrets embedded in ordinary source or Git history.'])
        result['revision'] = _digest(result)
        return result

    def _copy_history(self, source, gitdir, common, target):
        """Copy only object/ref data into a new config-free repository, never run Git in source."""
        total = count = 0
        for relative in ('objects', 'refs'):
            base = common / relative
            if not base.exists():
                continue
            for parent, dirs, files in os.walk(base, followlinks=False):
                for name in dirs + files:
                    item = Path(parent) / name
                    if item.is_symlink():
                        raise Problem('Git storage contains symbolic links; export a complete bundle instead')
                for name in files:
                    item = Path(parent) / name
                    # No alternates, grafts, attributes, hooks or configuration enter the new repository.
                    if item.relative_to(common).as_posix().startswith('objects/info/'):
                        continue
                    data, _ = _read_regular(item, MAX_GIT_BYTES)
                    total += len(data)
                    count += 1
                    if total > MAX_GIT_BYTES or count > MAX_ENTRIES:
                        raise Problem('Git history exceeds the 512 MiB or 30,000-file import limit')
                    dest = target / '.git' / item.relative_to(common)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
        for name, base, maximum in [('packed-refs', common, 2 * 1024 * 1024), ('HEAD', gitdir, 4096)]:
            if (base / name).exists():
                data, _ = _read_regular(base / name, maximum)
                (target / '.git' / name).write_bytes(data)
        self.engine.git(target, 'fsck', '--full', '--no-reflogs')

    def import_folder(self, path, name=None, expected_revision=None, allow_partial=False):
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 120):
            raise Problem('Name must contain 1–120 characters')
        if not isinstance(allow_partial, bool):
            raise Problem('allowPartial must be true or false')
        with self.engine.lock:
            before = self.inventory(path)
            if expected_revision != before['revision']:
                raise Problem('Folder inventory changed or is missing. Review it again before importing.')
            if before['partial'] and not allow_partial:
                raise Problem('Some files or history will be omitted. Review the inventory and explicitly accept the partial copy.')
            source = self._source(path)
            pid = uuid.uuid4().hex
            with tempfile.TemporaryDirectory(prefix='.adopt-', dir=self.engine.home) as temporary:
                stage = Path(temporary) / 'project'
                stage.mkdir()
                copied, skipped, total = self._scan(source, stage)
                if (copied, skipped, total) != (before['files'], before['skipped'], before['totalBytes']):
                    raise Problem('Source changed during import. Review the folder again.')
                self.engine.git(stage, 'init', '--template=', '-b', 'main')
                layout, gitdir, common = self._git_layout(source)
                if layout != before['git']:
                    raise Problem('Git history changed during import. Review the folder again.')
                if layout['history'] == 'preserved':
                    self._copy_history(source, gitdir, common, stage)
                # Preserve every original branch; the snapshot is a separate branch.
                head = self.engine.git(stage, 'rev-parse', '--verify', 'HEAD', allowed_returncodes=(0, 128)).strip()
                branch = 'refs/heads/unforge-import-' + pid[:12]
                self.engine.git(stage, 'symbolic-ref', 'HEAD', branch)
                if re.fullmatch(b'[a-f0-9]{40,64}', head):
                    self.engine.git(stage, 'update-ref', branch, head.decode())
                    self.engine.git(stage, 'read-tree', head.decode())
                marker = stage / '.unforge' / 'project.json'
                marker.parent.mkdir(exist_ok=True)
                marker.write_text(json.dumps(dict(name=(name or source.name or 'Imported app').strip(),
                    description='Independent source copy imported from a local folder', schemaVersion=1)))
                # Force-add only reviewed files, even if the source's .gitignore excludes them.
                self.engine.git(stage, 'add', '-A', '--', '.')
                paths = [record['path'] for record in copied] + ['.unforge/project.json']
                for start in range(0, len(paths), 200):
                    self.engine.git(stage, 'add', '-f', '--', *paths[start:start + 200])
                self.engine.git(stage, 'commit', '--allow-empty', '-m', 'Import reviewed independent source snapshot')
                self.engine.validate_checkout_size(stage, 'HEAD')
                self.engine.validate_tree(stage, 'HEAD')
                after = self.inventory(path)
                if before['revision'] != after['revision']:
                    raise Problem('Source changed during import. No project was added; review again.')
                receipt = dict(before, projectId=pid, importedHead=self.engine.git(stage, 'rev-parse', 'HEAD').decode().strip())
                # Source paths and omission receipts stay local, outside portable source history.
                (stage / '.git' / 'unforge-adoption.json').write_text(json.dumps(receipt))
                stage.rename(self.engine.home / pid)
            return dict(project=self.engine.detail(pid), adoption=receipt)

    def adoption(self, pid):
        marker = self.engine.root(pid) / '.git' / 'unforge-adoption.json'
        return json.loads(_read_regular(marker, 8 * 1024 * 1024)[0]) if marker.exists() else None

    def files(self, pid, cursor=0, limit=100):
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise Problem('File cursor must be a non-negative integer')
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise Problem('File page size must be between 1 and 500')
        with self.engine.lock:
            root = self.engine.root(pid)
            raw = self.engine.git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard')
            if len(raw) > 8 * 1024 * 1024:
                raise Problem('Project file inventory exceeds the supported limit')
            names = sorted(set(filter(None, raw.decode('utf-8').split('\0'))))
            if len(names) > MAX_ENTRIES:
                raise Problem('Project has more than 30,000 source entries')
            page = []
            for name in names[cursor:cursor + limit]:
                entry = dict(path=name, readonly=name.lower() == '.unforge/project.json')
                try:
                    target = self.engine.safe_path(root, name)
                    info = target.stat()
                    entry.update(size=info.st_size, readable=stat.S_ISREG(info.st_mode) and info.st_size <= MAX_TEXT)
                    if not entry['readable']:
                        entry['reason'] = 'File is too large for the text editor or is not a regular file'
                except (OSError, Problem) as error:
                    entry.update(readable=False, reason=str(error))
                page.append(entry)
            next_cursor = cursor + limit if cursor + limit < len(names) else None
            return dict(files=page, total=len(names), cursor=cursor, nextCursor=next_cursor,
                        revision=_digest(names))

    def read_file(self, pid, path):
        with self.engine.lock:
            root = self.engine.root(pid)
            target = self.engine.safe_path(root, path)
            names = self.engine.git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', path).decode().split('\0')
            if path not in names:
                raise Problem('This file is not part of the visible project source')
            data, _ = _read_regular(target, MAX_TEXT)
            try:
                content = data.decode('utf-8')
                if '\0' in content:
                    raise UnicodeError()
            except UnicodeError as error:
                raise Problem('This is a binary file; use an external editor') from error
            return dict(path=path, content=content, sha256=hashlib.sha256(data).hexdigest(),
                        readonly=path.lower() == '.unforge/project.json')
