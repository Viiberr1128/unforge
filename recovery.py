"""Portable, bounded source and explicitly declared local-data recovery capsules."""
import hashlib
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import quote
import uuid
import unicodedata

from engine import Engine, Problem

LIMIT = 256 * 1024 * 1024
FILE_LIMIT = 10000
OMISSIONS = ['Undeclared local data', 'External databases and hosted services',
             'Separately stored credentials', 'External Git LFS objects',
             'Unsaved or untracked source files']
WARNINGS = ['Recovery capsules are not encrypted. Declared data can contain private information; keep the archive somewhere you control.',
            'The source bundle contains Git history, which can include previously committed private content.',
            'Rehearsal verifies reconstruction and integrity, not application behavior or hosted-service recovery.']


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def overlaps(left, right):
    left, right = (unicodedata.normalize('NFC', value).casefold() for value in (left, right))
    return left == right or left.startswith(right + '/') or right.startswith(left + '/')


def register_path(registry, path, kind='file'):
    """Reject aliases of directory prefixes as well as complete filenames."""
    parts = path.split('/')
    for index in range(1, len(parts) + 1):
        prefix = '/'.join(parts[:index])
        entry_kind = kind if index == len(parts) else 'directory'
        key = unicodedata.normalize('NFC', prefix).casefold()
        previous = registry.get(key)
        if previous and previous != (prefix, entry_kind):
            raise Problem('Recovery paths collide by case, Unicode normalization, or file type')
        registry[key] = (prefix, entry_kind)


class Recovery:
    def __init__(self, engine):
        self.engine = engine
        self.home = engine.home / '.recovery'
        if self.home.is_symlink():
            raise Problem('Recovery storage cannot be a symbolic link')
        self.home.mkdir(exist_ok=True, mode=0o700)

    def _path(self, root, path):
        target = self.engine.safe_path(root, path)
        if any(part.casefold() == '.unforge' for part in Path(path).parts):
            raise Problem('Recovery assets cannot use project metadata paths')
        return target

    def _assets(self, root, assets, tracked):
        if not isinstance(assets, list) or len(assets) > 100:
            raise Problem('Declare at most 100 recovery assets')
        result = []
        portable_paths = {}
        for path in tracked:
            register_path(portable_paths, path)
        for asset in assets:
            if not isinstance(asset, dict) or asset.get('kind') not in ('file', 'directory', 'sqlite'):
                raise Problem('Asset kind must be file, directory, or sqlite')
            path = asset.get('path')
            self._path(root, path)
            if any(overlaps(path, existing['path']) for existing in result):
                raise Problem('Recovery assets overlap')
            if any(overlaps(path, source) for source in tracked):
                raise Problem('Recovery assets cannot collide with tracked source files')
            register_path(portable_paths, path, 'directory' if asset['kind'] == 'directory' else 'file')
            result.append({'path': path, 'kind': asset['kind']})
        return result

    def _inventory(self, root, source):
        """A second inventory catches additions, removals, and writes during capture."""
        inventory = {}
        normalized_names = set()
        portable_paths = {}
        def walk_error(error):
            raise Problem('A data directory could not be read: ' + str(error)) from error
        for base, dirs, names in os.walk(source, followlinks=False, onerror=walk_error):
            for name in dirs + names:
                item = Path(base) / name
                relative = item.relative_to(root).as_posix()
                self._path(root, relative)
                key = unicodedata.normalize('NFC', relative).casefold()
                if key in normalized_names:
                    raise Problem('Data paths collide by case or Unicode normalization')
                normalized_names.add(key)
                metadata = item.lstat()
                if not stat.S_ISREG(metadata.st_mode) and not stat.S_ISDIR(metadata.st_mode):
                    raise Problem('Symbolic links and special files cannot be captured')
                register_path(portable_paths, relative, 'directory' if stat.S_ISDIR(metadata.st_mode) else 'file')
                inventory[relative] = (metadata.st_mode, metadata.st_size, metadata.st_mtime_ns, metadata.st_ino)
                if len(inventory) > FILE_LIMIT:
                    raise Problem('Recovery capsule exceeds 10,000 entries')
        return inventory

    def _sqlite(self, source, target=None):
        deadline = time.monotonic() + 15
        connection = None
        try:
            connection = sqlite3.connect('file:' + quote(str(source.resolve())) + '?mode=ro', uri=True, timeout=5)
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            page_size = connection.execute('PRAGMA page_size').fetchone()[0]
            if connection.execute('PRAGMA page_count').fetchone()[0] * page_size > LIMIT:
                raise Problem('SQLite asset exceeds 256 MiB')
            if target is not None:
                with closing(sqlite3.connect(target)) as output:
                    def progress(status, remaining, total):
                        if time.monotonic() > deadline:
                            raise Problem('SQLite backup exceeded its 15-second time limit')
                        if total * page_size > LIMIT:
                            raise Problem('SQLite asset exceeds 256 MiB')
                    connection.backup(output, pages=128, progress=progress, sleep=0.05)
                    output.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
                    results = output.execute('PRAGMA integrity_check').fetchall()
            else:
                results = connection.execute('PRAGMA integrity_check').fetchall()
            if results != [('ok',)]:
                raise Problem('SQLite integrity check failed')
        except sqlite3.Error as error:
            raise Problem('SQLite recovery asset failed: ' + str(error)) from error
        finally:
            if connection is not None:
                connection.close()

    def _copy_file(self, source, target):
        descriptor = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(descriptor, 'rb') as reader:
            before = os.fstat(reader.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > LIMIT:
                raise Problem('Recovery assets must be bounded regular files')
            copied = 0
            with target.open('xb') as writer:
                for block in iter(lambda: reader.read(1024 * 1024), b''):
                    copied += len(block)
                    if copied > LIMIT:
                        raise Problem('Recovery asset exceeds 256 MiB')
                    writer.write(block)
            after = source.lstat()
            fingerprint = lambda value: (value.st_size, value.st_mtime_ns, value.st_ino, value.st_mode)
            if copied != before.st_size or fingerprint(before) != fingerprint(after):
                raise Problem('An asset changed during capture. Pause writes and try again.')

    def _store(self, pid):
        self.engine.root(pid)
        folder = self.home / pid
        if folder.is_symlink():
            raise Problem('Invalid recovery storage')
        folder.mkdir(exist_ok=True, mode=0o700)
        return folder

    def create(self, pid, assets):
        with self.engine.lock:
            root = self.engine.root(pid)
            # Declared ignored assets may exist, but source must be fully saved.
            if self.engine.git(root, 'status', '--porcelain'):
                raise Problem('Save source changes before making a recovery capsule; data assets should be ignored by Git')
            self.engine.validate_tree(root, 'HEAD')
            self.engine.validate_checkout_size(root, 'HEAD')
            tracked = list(filter(None, self.engine.git(root, 'ls-files', '-z').decode('utf-8').split('\0')))
            assets = self._assets(root, assets, tracked)
            head = self.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
            capsule_id = uuid.uuid4().hex
            folder = self._store(pid)
            with tempfile.TemporaryDirectory(prefix='capture-', dir=self.home) as temporary:
                stage = Path(temporary)
                bundle = stage / 'project.bundle'
                self.engine.git(root, 'bundle', 'create', str(bundle), '--all')
                total = bundle.stat().st_size
                if total > LIMIT:
                    raise Problem('Recovery capsule exceeds 256 MiB')
                files = {'project.bundle': digest(bundle)}
                directories = []
                inventories = []
                for asset in assets:
                    source = self._path(root, asset['path'])
                    ignore_path = asset['path'] + ('/' if asset['kind'] == 'directory' else '')
                    if not self.engine.git(root, 'check-ignore', '--', ignore_path, allowed_returncodes=(0, 1)):
                        raise Problem('Declare data paths in .gitignore and save it before capturing them')
                    if asset['kind'] == 'directory':
                        if not source.is_dir():
                            raise Problem('Declared directory does not exist')
                        candidates = []
                        directories.append('assets/' + asset['path'])
                        inventory = self._inventory(root, source)
                        inventories.append((source, inventory))
                        for relative, info in inventory.items():
                            if stat.S_ISDIR(info[0]):
                                directories.append('assets/' + relative)
                            else:
                                candidates.append(root / relative)
                            if len(candidates) + len(directories) + len(files) > FILE_LIMIT:
                                raise Problem('Recovery capsule exceeds 10,000 entries')
                    else:
                        if not source.is_file():
                            raise Problem('Declared file does not exist')
                        candidates = [source]
                    for item in candidates:
                        relative = 'assets/' + item.relative_to(root).as_posix()
                        target = stage / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if asset['kind'] == 'sqlite':
                            if item.stat().st_size > LIMIT:
                                raise Problem('SQLite asset exceeds 256 MiB')
                            self._sqlite(item, target)
                        else:
                            if total + item.stat().st_size > LIMIT:
                                raise Problem('Recovery capsule exceeds 256 MiB')
                            self._copy_file(item, target)
                        total += target.stat().st_size
                        if total > LIMIT or len(files) + len(directories) >= FILE_LIMIT:
                            raise Problem('Recovery capsule exceeds its size or entry limit')
                        files[relative] = digest(target)
                for source, inventory in inventories:
                    if self._inventory(root, source) != inventory:
                        raise Problem('A data directory changed during capture. Pause writes and try again.')
                if self.engine.git(root, 'rev-parse', 'HEAD').decode().strip() != head or self.engine.git(root, 'status', '--porcelain'):
                    raise Problem('Source changed during capture. Retry after saving it.')
                manifest = {'format': 'unforge-recovery-v1', 'id': capsule_id, 'createdAt': datetime.now(timezone.utc).isoformat(),
                            'projectName': self.engine.metadata(root)['name'], 'sourceHead': head, 'assets': assets,
                            'directories': directories, 'files': files, 'sourceOnly': not assets, 'omissions': OMISSIONS,
                            'encryption': 'none', 'warnings': WARNINGS}
                (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
                archive = stage / 'capsule.tar.gz'
                with tarfile.open(archive, 'w:gz') as output:
                    for relative in ['manifest.json', *files]:
                        output.add(stage / relative, arcname=relative, recursive=False)
                if archive.stat().st_size > LIMIT:
                    raise Problem('Compressed recovery capsule exceeds 256 MiB')
                archive.chmod(0o600)
                receipt = self._receipt(manifest, archive)
                receipt_path = stage / 'receipt.json'
                receipt_path.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
                os.replace(archive, folder / (capsule_id + '.tar.gz'))
                os.replace(receipt_path, folder / (capsule_id + '.json'))
                return receipt

    def _receipt(self, manifest, archive):
        return {'id': manifest['id'], 'createdAt': manifest['createdAt'], 'projectName': manifest['projectName'],
                'sourceHead': manifest['sourceHead'], 'assets': manifest['assets'], 'sourceOnly': not manifest['assets'],
                'omissions': OMISSIONS, 'fileCount': len(manifest['files']), 'bytes': archive.stat().st_size,
                'sha256': digest(archive), 'kind': 'source-only' if not manifest['assets'] else 'source-and-declared-data',
                'encryption': 'none', 'warnings': WARNINGS}

    def _valid_receipt(self, receipt):
        if not isinstance(receipt, dict):
            return False
        for key, pattern in [('id', '[a-f0-9]{32}'), ('sha256', '[a-f0-9]{64}'), ('sourceHead', '[a-f0-9]{40,64}')]:
            if not isinstance(receipt.get(key), str) or not re.fullmatch(pattern, receipt[key]):
                return False
        if not isinstance(receipt.get('createdAt'), str) or len(receipt['createdAt']) > 80:
            return False
        if not isinstance(receipt.get('projectName'), str) or len(receipt['projectName']) > 120:
            return False
        assets = receipt.get('assets')
        return isinstance(assets, list) and len(assets) <= 100 and all(
            isinstance(asset, dict) and isinstance(asset.get('path'), str) and
            asset.get('kind') in ('file', 'directory', 'sqlite') for asset in assets)

    def list(self, pid):
        folder = self._store(pid)
        receipts = []
        for path in folder.glob('*.json'):
            if path.is_symlink():
                continue
            try:
                if path.stat().st_size > 256 * 1024:
                    continue
                receipt = json.loads(path.read_text(encoding='utf-8'))
                if not self._valid_receipt(receipt):
                    continue
                archive = folder / (receipt['id'] + '.tar.gz')
                if path.stem == receipt['id'] and archive.is_file() and not archive.is_symlink():
                    receipts.append(receipt)
            except (OSError, ValueError, TypeError, AttributeError, KeyError):
                continue
        return sorted(receipts, key=lambda value: value.get('createdAt', ''), reverse=True)

    def _archive(self, pid, capsule_id):
        if not isinstance(capsule_id, str) or not re.fullmatch(r'[a-f0-9]{32}', capsule_id):
            raise Problem('Invalid recovery capsule ID')
        folder = self._store(pid)
        archive = folder / (capsule_id + '.tar.gz')
        receipt = folder / (capsule_id + '.json')
        if archive.is_symlink() or receipt.is_symlink() or not archive.is_file() or not receipt.is_file():
            raise Problem('Recovery capsule not found')
        if archive.stat().st_size > LIMIT or receipt.stat().st_size > 256 * 1024:
            raise Problem('Recovery storage exceeds its size limit')
        try:
            data = json.loads(receipt.read_text(encoding='utf-8'))
            expected = data['sha256']
            if not self._valid_receipt(data) or data['id'] != capsule_id:
                raise ValueError('Malformed receipt')
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise Problem('Invalid recovery capsule receipt') from error
        if digest(archive) != expected:
            raise Problem('Recovery capsule hash does not match its receipt')
        return archive

    def download(self, pid, capsule_id):
        with self.engine.lock:
            archive = self._archive(pid, capsule_id)
            return 'unforge-recovery-' + capsule_id + '.tar.gz', archive.read_bytes()

    def _unpack(self, archive, stage):
        if archive.is_symlink() or not archive.is_file() or archive.stat().st_size > LIMIT:
            raise Problem('Capsule must be a regular file no larger than 256 MiB')
        total, count = 0, 0
        names = set()
        normalized_names = set()
        portable_paths = {}
        try:
            # Bound the gzip expansion before tarfile parses any PAX metadata.
            # This also bounds headers that never become ordinary archive members.
            with tempfile.TemporaryFile() as expanded:
                expanded_bytes = 0
                with gzip.open(archive, 'rb') as compressed:
                    for block in iter(lambda: compressed.read(1024 * 1024), b''):
                        expanded_bytes += len(block)
                        if expanded_bytes > LIMIT + (FILE_LIMIT + 1) * 4096 + 4 * 1024 * 1024:
                            raise Problem('Capsule exceeds decompression limits')
                        expanded.write(block)
                expanded.seek(0)
                with tarfile.open(fileobj=expanded, mode='r:') as source:
                    for member in source:
                        path = PurePosixPath(member.name)
                        if not member.isfile() or member.size < 0 or path.is_absolute() or any(p in ('', '.', '..') for p in member.name.split('/')) or '\\' in member.name or len(member.name) > 270 or any(ord(c) < 32 for c in member.name):
                            raise Problem('Unsafe capsule archive entry')
                        normalized = unicodedata.normalize('NFC', member.name).casefold()
                        if normalized in normalized_names:
                            raise Problem('Duplicate capsule path')
                        register_path(portable_paths, member.name)
                        count += 1
                        total += member.size
                        if total > LIMIT or count > FILE_LIMIT + 1 or (member.name == 'manifest.json' and member.size > 4 * 1024 * 1024):
                            raise Problem('Capsule exceeds extraction limits')
                        if member.name != 'manifest.json' and member.name != 'project.bundle' and not member.name.startswith('assets/'):
                            raise Problem('Unknown capsule entry')
                        target = stage.joinpath(*path.parts)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with source.extractfile(member) as reader, target.open('xb') as writer:
                            shutil.copyfileobj(reader, writer)
                        names.add(member.name)
                        normalized_names.add(normalized)
            manifest = json.loads((stage / 'manifest.json').read_text(encoding='utf-8'))
            if not isinstance(manifest, dict) or manifest.get('format') != 'unforge-recovery-v1' or not isinstance(manifest.get('files'), dict):
                raise Problem('Unsupported capsule manifest')
            for field, maximum in [('projectName', 120), ('createdAt', 80)]:
                if not isinstance(manifest.get(field), str) or not manifest[field].strip() or len(manifest[field]) > maximum:
                    raise Problem('Invalid capsule ' + field)
            if not isinstance(manifest.get('id'), str) or not re.fullmatch('[a-f0-9]{32}', manifest['id']):
                raise Problem('Invalid capsule ID')
            if set(manifest['files']) | {'manifest.json'} != names or 'project.bundle' not in manifest['files']:
                raise Problem('Capsule manifest does not cover its contents')
            for relative, expected in manifest['files'].items():
                if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected):
                    raise Problem('Invalid capsule content hash')
                if digest(stage / relative) != expected:
                    raise Problem('Capsule content failed hash verification')
            if not isinstance(manifest.get('directories', []), list) or len(manifest.get('directories', [])) > FILE_LIMIT:
                raise Problem('Invalid capsule directories')
            return manifest
        except Problem:
            raise
        except (tarfile.TarError, OSError, EOFError, ValueError, KeyError, TypeError, RecursionError) as error:
            raise Problem('Invalid recovery capsule: ' + str(error)) from error

    def _reconstruct(self, archive, engine, recovered_name=False):
        project = None
        with tempfile.TemporaryDirectory(prefix='recovery-verify-') as temporary:
            stage = Path(temporary)
            manifest = self._unpack(archive, stage)
            try:
                head = manifest.get('sourceHead')
                if not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40,64}', head):
                    raise Problem('Invalid capsule source revision')
                heads = engine.git(stage, 'bundle', 'list-heads', str(stage / 'project.bundle')).decode('utf-8').splitlines()
                if head + ' HEAD' not in heads:
                    raise Problem('Capsule revision does not match the bundled source')
                suffix = ' (recovered)'
                name = manifest['projectName'].strip()[:80 - len(suffix)].rstrip() + suffix if recovered_name else None
                project = engine.import_bundle(str(stage / 'project.bundle'), name=name)
                root = engine.root(project['id'])
                root.chmod(0o700)
                tracked = list(filter(None, engine.git(root, 'ls-files', '-z').decode('utf-8').split('\0')))
                assets = self._assets(root, manifest.get('assets'), tracked)
                engine.git(root, 'merge-base', '--is-ancestor', head, 'HEAD')
                # Import may normalize Unforge metadata, but must not change any
                # recovered application source relative to the recorded revision.
                engine.git(root, 'diff', '--quiet', head, 'HEAD', '--', '.', ':(exclude).unforge/project.json')
                allowed_files = set()
                for asset in assets:
                    ignore_path = asset['path'] + ('/' if asset['kind'] == 'directory' else '')
                    if not engine.git(root, 'check-ignore', '--', ignore_path, allowed_returncodes=(0, 1)):
                        raise Problem('Capsule data must be ignored by the bundled source')
                    relative = 'assets/' + asset['path']
                    if asset['kind'] == 'directory':
                        allowed_files.update(name for name in manifest['files'] if name.startswith(relative + '/'))
                    else:
                        if relative not in manifest['files']:
                            raise Problem('Declared recovery asset is missing')
                        allowed_files.add(relative)
                if allowed_files != set(manifest['files']) - {'project.bundle'}:
                    raise Problem('Capsule contains undeclared assets')
                seen_directories = set()
                portable_paths = {}
                for path in [*tracked, *(relative[len('assets/'):] for relative in allowed_files)]:
                    register_path(portable_paths, path)
                for directory in manifest.get('directories', []):
                    if not isinstance(directory, str) or not directory.startswith('assets/'):
                        raise Problem('Invalid capsule directory')
                    relative = directory[len('assets/'):]
                    normalized = unicodedata.normalize('NFC', relative).casefold()
                    if normalized in seen_directories:
                        raise Problem('Duplicate capsule directory')
                    seen_directories.add(normalized)
                    register_path(portable_paths, relative, 'directory')
                    if not any(a['kind'] == 'directory' and (relative == a['path'] or relative.startswith(a['path'] + '/')) for a in assets):
                        raise Problem('Undeclared capsule directory')
                    self._path(root, relative).mkdir(parents=True, exist_ok=True)
                for relative in sorted(allowed_files):
                    target = self._path(root, relative[len('assets/'):])
                    if target.exists():
                        raise Problem('Recovery would overwrite a source file')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(stage / relative, target)
                    if digest(target) != manifest['files'][relative]:
                        raise Problem('Restored asset failed hash verification')
                for asset in assets:
                    if asset['kind'] == 'sqlite':
                        self._sqlite(self._path(root, asset['path']))
                    if asset['kind'] == 'directory' and not self._path(root, asset['path']).is_dir():
                        raise Problem('Declared recovery directory is missing')
                if engine.git(root, 'status', '--porcelain'):
                    raise Problem('Restored data would enter saved source versions')
                engine.git(root, 'fsck', '--full', '--no-reflogs')
                return engine.detail(project['id']), manifest
            except Exception:
                if project is not None:
                    shutil.rmtree(engine.home / project['id'])
                raise

    def _atomic_json(self, destination, value):
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='.receipt-', dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                json.dump(value, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

    def _adopt(self, pid, archive, manifest):
        """Keep an imported capsule available after its original file is removed."""
        folder = self._store(pid)
        capsule_id = manifest['id']
        with tempfile.TemporaryDirectory(prefix='adopt-', dir=self.home) as temporary:
            copied = Path(temporary) / 'capsule.tar.gz'
            self._copy_file(archive, copied)
            copied.chmod(0o600)
            receipt = self._receipt(manifest, copied)
            os.replace(copied, folder / (capsule_id + '.tar.gz'))
            self._atomic_json(folder / (capsule_id + '.json'), receipt)

    def _materialize(self, archive):
        detail, manifest = self._reconstruct(archive, self.engine, recovered_name=True)
        try:
            self._adopt(detail['id'], archive, manifest)
            return detail
        except Exception:
            # Only the newly created project is removed on incomplete import.
            shutil.rmtree(self.engine.home / detail['id'])
            shutil.rmtree(self.home / detail['id'], ignore_errors=True)
            raise

    def restore(self, pid, capsule_id):
        with self.engine.lock:
            return self._materialize(self._archive(pid, capsule_id))

    def rehearse(self, pid, capsule_id):
        with self.engine.lock:
            archive = self._archive(pid, capsule_id)
            expected_hash = digest(archive)
            with tempfile.TemporaryDirectory(prefix='unforge-rehearsal-') as temporary:
                _, manifest = self._reconstruct(archive, Engine(temporary))
            if digest(self._archive(pid, capsule_id)) != expected_hash:
                raise Problem('Capsule changed during rehearsal')
            evidence = {'id': uuid.uuid4().hex, 'capsuleId': capsule_id, 'capsuleSha256': expected_hash,
                    'sourceHead': manifest['sourceHead'], 'verifiedAt': datetime.now(timezone.utc).isoformat(),
                    'ok': True, 'sourceOnly': not manifest['assets'],
                    'verifiedFiles': len(manifest['files']), 'sqliteChecks': sum(a['kind'] == 'sqlite' for a in manifest['assets']),
                    'checks': ['manifest hashes', 'Git reconstruction and integrity'] +
                              (['declared asset hashes'] if manifest['assets'] else []) +
                              (['declared SQLite integrity'] if any(a['kind'] == 'sqlite' for a in manifest['assets']) else []),
                    'applicationExecuted': False, 'omissions': OMISSIONS,
                    'warnings': WARNINGS, 'encryption': 'none',
                    'scope': 'Source-only reconstruction' if not manifest['assets'] else 'Source and declared local data reconstruction'}
            folder = self._rehearsal_folder(pid)
            self._atomic_json(folder / (evidence['id'] + '.json'), evidence)
            return evidence

    def _rehearsal_folder(self, pid):
        folder = self._store(pid) / 'rehearsals'
        if folder.is_symlink():
            raise Problem('Invalid recovery rehearsal storage')
        folder.mkdir(exist_ok=True, mode=0o700)
        return folder

    def rehearsals(self, pid):
        with self.engine.lock:
            folder = self._rehearsal_folder(pid)
            receipts, verified = [], {}
            for path in folder.glob('*.json'):
                try:
                    if path.is_symlink() or path.stat().st_size > 256 * 1024:
                        continue
                    receipt = json.loads(path.read_text(encoding='utf-8'))
                    if not isinstance(receipt, dict) or receipt.get('id') != path.stem or not re.fullmatch('[a-f0-9]{32}', path.stem):
                        continue
                    if not isinstance(receipt.get('verifiedAt'), str) or not isinstance(receipt.get('sourceHead'), str):
                        continue
                    capsule_id = receipt['capsuleId']
                    if capsule_id not in verified:
                        verified[capsule_id] = digest(self._archive(pid, capsule_id))
                    if receipt.get('ok') is True and receipt.get('applicationExecuted') is False and receipt.get('capsuleSha256') == verified[capsule_id]:
                        receipts.append(receipt)
                except (Problem, OSError, ValueError, TypeError, KeyError):
                    continue
            return sorted(receipts, key=lambda receipt: receipt.get('verifiedAt', ''), reverse=True)

    def state(self, pid):
        with self.engine.lock:
            return {'capsules': self.list(pid), 'rehearsals': self.rehearsals(pid)}

    def import_capsule(self, path):
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise Problem('Choose an absolute local recovery capsule path')
        with self.engine.lock, tempfile.TemporaryDirectory(prefix='unforge-recovery-import-') as temporary:
            source = Path(path)
            if source.is_symlink() or not source.is_file() or source.stat().st_size > LIMIT:
                raise Problem('Capsule must be a regular file no larger than 256 MiB')
            snapshot = Path(temporary) / 'capsule.tar.gz'
            self._copy_file(source, snapshot)
            return self._materialize(snapshot)
