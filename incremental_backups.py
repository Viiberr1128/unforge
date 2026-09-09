"""Append-only Restic vaults, with immutable descriptors for exact recovery points.

API: IncrementalBackups(Backups).backup(job, destinations=None) is a synchronous
worker; restore(point_path, password, new_destination) returns a restore receipt.
inspect(point_path) validates the point and returns its full requiredFiles list,
vaultPath and repositoryPath. Cloud upload/download proof must cover that full
vault, followed by inspect and an exact-snapshot restore, never just POINT.json.

The private Restic writer lives under .backups/incremental. Sync destinations
receive immutable encrypted objects only; Restic never writes in a sync folder.
Generations seal at 500 snapshots or 4,000 encrypted file/directory entries.
A new vault then takes one new full seed; old vaults are preserved. No pruning,
adoption of another writer, key rotation or v1 migration is implicit.
Existing standalone .ufbackup points remain handled by Backups.restore. A .ufpoint
is NOT standalone: retain its entire .ufvault and a separately held passphrase.
Descriptors reveal opaque identifiers, ciphertext hashes/sizes and source counts,
not filenames or credentials. Restic provides all encryption/authentication.

One private writer is supported. OS locks exclude concurrent local processes;
a vault ownership record rejects other writers. Cloud synchronization is not a
distributed lock: copying private writer state onto a second active Mac is not a
supported way to collaborate. All earlier packs are retained. New points record
a conservative cumulative object inventory, so metadata and audit I/O grow with
history even though unchanged file contents are deduplicated. Vault publication
requires atomic hard-link installation (including normal APFS sync folders);
unsupported destination filesystems fail without replacing existing objects.
Shared-pack damage can affect several points: keep independent destination copies.
"""
from contextlib import contextmanager, ExitStack
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from backups import (Backups, MAX_BYTES, MAX_FILES, STORAGE_METADATA_ALLOWANCE,
                     atomic, digest, estimated_workspace_storage, real_directory,
                     regular_reader, stamp, storage_guard, sync_directory, sync_tree)
from engine import Problem

HEX = r'[a-f0-9]{64}'
OBJECT = re.compile(r'(?:config|(?:keys|index)/' + HEX + r'|data/[a-f0-9]{2}/' + HEX + r')\Z')
MAX_INVENTORY_BYTES = 64 * 1024**2
MAX_VAULT_ENTRIES = 8000  # Below the cloud helper's 10,000-entry horizon.
MAX_REPOSITORY_BYTES = 1024**4  # Explicit bound, not permission to consume this space.


def _json(path):
    with regular_reader(path) as reader:
        raw = reader.read(MAX_INVENTORY_BYTES + 1)
    if len(raw) > MAX_INVENTORY_BYTES: raise Problem('Recovery metadata exceeds its supported size.')
    try: result = json.loads(raw)
    except (ValueError, UnicodeError) as error: raise Problem('Invalid incremental recovery metadata.') from error
    if not isinstance(result, dict): raise Problem('Invalid incremental recovery metadata.')
    return result


def _mkdir(path):
    """Create only within already validated ancestors, rejecting link redirects."""
    path = Path(path)
    if not path.exists():
        _mkdir(path.parent)
        try: path.mkdir(mode=0o700)
        except FileExistsError: pass
        sync_directory(path.parent)
    real_directory(path)


@contextmanager
def _lease(path):
    real_directory(path.parent)
    try: fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as error: raise Problem('Cannot safely open the backup writer lock.') from error
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode): raise Problem('Invalid backup writer lock.')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error: raise Problem('Another incremental backup writer is active. Retry when it finishes.') from error
        yield
    finally: os.close(fd)


class IncrementalBackups:
    def __init__(self, service, *, max_points=500, max_entries=4000):
        if type(max_points) is not int or not 1 <= max_points <= 500 or type(max_entries) is not int or not 1 <= max_entries <= 4000:
            raise Problem('Invalid incremental vault rollover limits.')
        self.max_points, self.max_entries = max_points, max_entries
        self.service = service
        self.pointer = service.root / 'incremental-active.json'
        self.generations = service.root / 'incremental-generations'
        self.generation = 'legacy'
        self.rollover = None
        self.root = service.root / 'incremental'
        _mkdir(self.root)
        self.repository = self.root / 'repository'

    def _select_generation(self):
        if not self.pointer.exists() and not self.pointer.is_symlink():
            if self.generations.exists() or (self.service.root / 'incremental/SEALED.json').exists():
                raise Problem('The active backup generation pointer is missing. Existing vaults are preserved and can still be restored; repair the writer metadata before making new copies.')
            generation = 'legacy'
        else:
            value = _json(self.pointer)
            generation = value.get('generation')
            if value.get('schema') != 1 or not isinstance(generation, str) or not (generation == 'legacy' or re.fullmatch(r'[a-f0-9]{32}', generation)):
                raise Problem('The active backup generation pointer is damaged. Existing recovery vaults are unchanged.')
        root = self.service.root / 'incremental' if generation == 'legacy' else self.generations / generation
        # An existing pointer must never recreate a missing generation silently.
        real_directory(root)
        self.generation, self.root, self.repository = generation, root, root / 'repository'

    def _entry_paths(self, root, limit=MAX_VAULT_ENTRIES):
        result = set()
        if not root.exists(): return result
        real_directory(root)
        for parent, dirs, files in os.walk(root, followlinks=False):
            real_directory(Path(parent))
            for name in dirs + files:
                path = Path(parent) / name
                if path.is_symlink(): raise Problem('Recovery vault entries cannot be symbolic links.')
                result.add(path.relative_to(root).as_posix())
                if len(result) > limit: raise Problem('This recovery vault exceeds its supported entry limit. Earlier vaults are preserved.')
        return result

    def _rollover_needed(self):
        if (self.root / 'SEALED.json').exists() or (self.root / 'SEALED.json').is_symlink(): return True
        if not self.repository.exists(): return False
        entries = self._entry_paths(self.repository, MAX_FILES)
        points = sum(1 for name in entries if re.fullmatch('snapshots/' + HEX, name))
        return points >= self.max_points or len(entries) >= self.max_entries

    def _rotate(self):
        previous = self.root
        marker = previous / 'SEALED.json'
        if marker.exists() or marker.is_symlink():
            seal = _json(marker)
            next_id = seal.get('nextGeneration')
            if seal.get('schema') != 1 or not isinstance(next_id, str) or not re.fullmatch(r'[a-f0-9]{32}', next_id):
                raise Problem('The sealed backup generation record is damaged. Existing vaults are preserved.')
            next_root = self.generations / next_id
            real_directory(next_root)
            identity = _json(next_root / 'identity.json')
            if identity.get('schema') != 1 or not isinstance(identity.get('writerId'), str) or not re.fullmatch(r'[a-f0-9]{32}', identity['writerId']):
                raise Problem('The next backup generation identity is damaged.')
        else:
            next_id = uuid.uuid4().hex
            _mkdir(self.generations)
            next_root = self.generations / next_id; _mkdir(next_root)
            atomic(next_root / 'identity.json', {'schema': 1, 'writerId': uuid.uuid4().hex})
            # Both the target generation and seal are durable before selecting it.
            # Failure before the pointer swap leaves the old generation selected;
            # its seal tells the next attempt exactly which generation to resume.
            seal = {'schema': 1, 'nextGeneration': next_id, 'sealedAt': stamp()}
            atomic(marker, seal)
        atomic(self.pointer, {'schema': 1, 'generation': next_id})
        self._select_generation()
        self.rollover = {'sealedPreviousGeneration': True, 'generation': next_id,
                         'reason': 'A new vault keeps recovery inventories within supported limits. Previous vaults remain unchanged.',
                         'newSeedRequired': True}

    @contextmanager
    def _writer(self):
        # The stable lock must outlive every generation and pointer transition.
        with _lease(self.service.root / 'incremental-writer.lock'), ExitStack() as held:
            self._select_generation()
            held.enter_context(_lease(self.root / 'writer.lock'))
            if not self.pointer.exists(): atomic(self.pointer, {'schema': 1, 'generation': self.generation})
            if self._rollover_needed():
                # Verify the old repository/key before sealing it. This prevents
                # an accidental bad key from silently beginning a new lineage.
                self._identity(); self._initialize()
                self._rotate()
                held.enter_context(_lease(self.root / 'writer.lock'))
            yield

    def _cancel(self):
        if self.service.closed.is_set(): raise Problem('Incremental backup cancelled.')

    def _identity(self):
        path = self.root / 'identity.json'
        if not path.exists() and not path.is_symlink():
            # This is private writer metadata, never restored with the workspace.
            atomic(path, {'schema': 1, 'writerId': uuid.uuid4().hex})
        value = _json(path)
        if value.get('schema') != 1 or not re.fullmatch(r'[a-f0-9]{32}', str(value.get('writerId', ''))):
            raise Problem('Private backup writer identity is damaged. Existing recovery vaults are unchanged.')
        return value['writerId']

    def _initialize(self):
        s = self.service
        if not self.repository.exists() and not self.repository.is_symlink():
            with tempfile.TemporaryDirectory(prefix='initialize-', dir=self.root) as tmp:
                staged = Path(tmp) / 'repository'
                s._command(staged, s.key_path, 'init', '--repository-version', '2')
                sync_tree(staged)
                os.rename(staged, self.repository)
                sync_directory(self.root)
        real_directory(self.repository)
        config = json.loads(s._command(self.repository, s.key_path, 'cat', 'config'))
        repository_id = config.get('id') if isinstance(config, dict) else None
        if not isinstance(repository_id, str) or not re.fullmatch(HEX, repository_id):
            raise Problem('Restic did not return a valid repository identity.')
        return repository_id

    def _entry(self, path, relative):
        with regular_reader(path) as reader: size = os.fstat(reader.fileno()).st_size
        return {'path': relative, 'bytes': size, 'sha256': digest(path, self.service.closed)}

    def _inventory(self, snapshot):
        files = []
        for parent, dirs, names in os.walk(self.repository, followlinks=False):
            real_directory(Path(parent))
            if any((Path(parent) / name).is_symlink() for name in dirs):
                raise Problem('Private Restic directories cannot be symbolic links.')
            dirs[:] = sorted(name for name in dirs if name not in ('locks', 'tmp', 'snapshots'))
            for name in sorted(names):
                path = Path(parent) / name
                relative = path.relative_to(self.repository).as_posix()
                if not OBJECT.fullmatch(relative):
                    raise Problem('Unexpected object in private Restic repository: ' + relative)
                files.append(self._entry(path, relative))
                if len(files) > MAX_FILES: raise Problem('Recovery vault has too many encrypted objects.')
        files.append(self._entry(self.repository / 'snapshots' / snapshot, 'snapshots/' + snapshot))
        return sorted(files, key=lambda item: item['path'])

    def _validate_entry(self, root, entry):
        self._cancel()
        path = root / entry['path']
        with regular_reader(path) as reader:
            if os.fstat(reader.fileno()).st_size != entry['bytes']:
                raise Problem('Recovery vault object is incomplete or damaged: ' + entry['path'])
        if digest(path, self.service.closed) != entry['sha256']:
            raise Problem('Recovery vault object is incomplete or damaged: ' + entry['path'])

    def _install(self, source, target, entry):
        """Write once: never replace an existing encrypted object, even on retry."""
        self._cancel()
        _mkdir(target.parent)
        if target.exists() or target.is_symlink():
            self._validate_entry(target.parent, {**entry, 'path': target.name})
            return False
        fd, temporary = tempfile.mkstemp(prefix='.incoming-', dir=target.parent)
        try:
            checksum = hashlib.sha256(); size = 0
            with os.fdopen(fd, 'wb') as out, regular_reader(source) as reader:
                while True:
                    self._cancel()
                    chunk = reader.read(1024 * 1024)
                    if not chunk: break
                    size += len(chunk); checksum.update(chunk); out.write(chunk)
                out.flush(); os.fsync(out.fileno())
            if size != entry['bytes'] or checksum.hexdigest() != entry['sha256']:
                raise Problem('Encrypted source changed during publication.')
            # link is an atomic no-replace installation, unlike rename/replace.
            try: os.link(temporary, target, follow_symlinks=False)
            except FileExistsError:
                self._validate_entry(target.parent, {**entry, 'path': target.name})
                return False
            except OSError as error:
                if error.errno in (errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP):
                    raise Problem('This destination cannot atomically publish immutable backup objects. Choose an APFS folder or another filesystem with hard-link support.') from error
                raise
            sync_directory(target.parent)
            return True
        finally:
            os.unlink(temporary)
            sync_directory(target.parent)

    def _write_once(self, target, value):
        with tempfile.TemporaryDirectory(prefix='metadata-', dir=self.root) as tmp:
            source = Path(tmp) / 'value.json'; atomic(source, value)
            return self._install(source, target, self._entry(source, target.name))

    def _publish(self, destination, descriptor):
        parent = Path(destination['path'])
        real_directory(parent)
        vault = parent / ('unforge-' + descriptor['repositoryId'] + '.ufvault')
        with _lease(parent / ('.unforge-writer-' + descriptor['repositoryId'] + '.lock')):
            _mkdir(vault)
            identity = {'schema': 2, 'repositoryId': descriptor['repositoryId'], 'writerId': descriptor['writerId']}
            if (vault / 'VAULT.json').exists() and _json(vault / 'VAULT.json') != identity:
                raise Problem('This recovery vault belongs to a different writer.')
            identity_added = self._write_once(vault / 'VAULT.json', identity)
            if _json(vault / 'VAULT.json') != identity:
                raise Problem('This recovery vault belongs to a different writer.')
            repository = vault / 'repository'; _mkdir(repository)
            points = vault / 'points'; _mkdir(points)
            point = points / (descriptor['pointId'] + '.ufpoint')
            if point.exists() or point.is_symlink(): raise Problem('A recovery point with this identifier already exists.')
            # Count both files and directories, including prior point descriptors.
            # A single unusually large snapshot may cross a limit even in a fresh
            # generation; reject it before publishing objects, never claim proof.
            projected = self._entry_paths(vault)
            for entry in descriptor['files']:
                relative = Path('repository') / entry['path']
                projected.add(relative.as_posix())
                projected.update(parent.as_posix() for parent in relative.parents if parent != Path('.'))
            projected.update({'points/' + point.name, 'points/' + point.name + '/POINT.json'})
            if len(projected) > MAX_VAULT_ENTRIES:
                raise Problem('This snapshot would exceed the supported recovery-vault entry limit. No recovery point was published; earlier vaults remain available.')
            missing = sum(entry['bytes'] + 4096 for entry in descriptor['files'] if not (repository / entry['path']).exists())
            storage_guard([(parent, missing + STORAGE_METADATA_ALLOWANCE, 'incremental encrypted publication')])
            added, reused = 0, 0
            # Snapshot commit marker is published only after its packs and indexes.
            ordered = sorted(descriptor['files'], key=lambda item: (item['path'].startswith('snapshots/'), item['path'].startswith('index/'), item['path']))
            for entry in ordered:
                created = self._install(self.repository / entry['path'], repository / entry['path'], entry)
                if created: added += entry['bytes']
                else: reused += entry['bytes']
            final_descriptor = {**descriptor, 'vaultIdentitySha256': digest(vault / 'VAULT.json', self.service.closed)}
            with tempfile.TemporaryDirectory(prefix='.pending-point-', dir=points) as tmp:
                staged = Path(tmp) / point.name; staged.mkdir(mode=0o700)
                atomic(staged / 'POINT.json', final_descriptor)
                sync_tree(staged)
                self._cancel()
                real_directory(points)
                if point.exists() or point.is_symlink(): raise Problem('Recovery point collision; existing point was preserved.')
                os.rename(staged, point)
                sync_directory(points)
            # Post-promotion readback covers every object required by this point.
            self.inspect(point)
            metadata_bytes = (point / 'POINT.json').stat().st_size
            if identity_added: metadata_bytes += (vault / 'VAULT.json').stat().st_size
            return {**destination, 'backupPath': str(point), 'vaultPath': str(vault),
                    'state': 'upload_pending' if destination.get('kind') in ('icloud', 'drive') else 'copied_locally',
                    'format': 'incremental-v2', 'readbackAt': stamp(), 'addedBytes': added + metadata_bytes,
                    'objectBytesAdded': added, 'metadataBytesAdded': metadata_bytes, 'reusedBytes': reused,
                    'vaultEntries': len(projected), 'vaultEntryLimit': MAX_VAULT_ENTRIES}

    def backup(self, job, destinations=None):
        """Called by Backups' owned worker. Failures persist and never prune points."""
        s = self.service
        self.rollover = None
        try:
            if not re.fullmatch(r'[a-f0-9]{32}', str(job.get('id', ''))): raise Problem('Invalid recovery point identifier.')
            with self._writer():
                self._cancel()
                destinations = destinations if destinations is not None else s.settings().get('destinations', [])
                if not destinations: raise Problem('Choose a recovery destination first.')
                for destination in destinations:
                    path = Path(destination['path']); real_directory(path)
                    if path.is_relative_to(s.engine.home) or s.engine.home.is_relative_to(path):
                        raise Problem('Recovery vaults must be outside the active workspace.')
                writer = self._identity(); repository_id = self._initialize()
                if self.rollover: job['vaultRollover'] = dict(self.rollover)
                job.update(format='incremental-v2', repositoryId=repository_id, destinations=[], phase='Capturing workspace for incremental backup')
                s._record(job)
                def capacity_check(listing):
                    estimate = estimated_workspace_storage(listing)
                    job['storagePlan'] = storage_guard([(self.root, estimate * 4, 'source capture, new encrypted data, and recovery verification')])
                with tempfile.TemporaryDirectory(prefix='snapshot-', dir=self.root) as tmp:
                    temp = Path(tmp); workspace = temp / 'workspace'; workspace.mkdir()
                    with s.engine.lock: manifest = s._copy_workspace(workspace, capacity_check=capacity_check)
                    manifest['incrementalPoint'] = {'pointId': job['id'], 'repositoryId': repository_id}
                    atomic(workspace / 'UNFORGE-RECOVERY.json', manifest)
                    manifest_hash = digest(workspace / 'UNFORGE-RECOVERY.json', s.closed)
                    output = s._command(self.repository, s.key_path, 'backup', '--force', '--host', 'unforge', '--tag', job['id'], 'workspace', cwd=temp)
                    summaries = [json.loads(line) for line in output.splitlines() if line.strip()]
                    snapshot = next((row.get('snapshot_id') for row in reversed(summaries) if row.get('message_type') == 'summary'), None)
                    if not isinstance(snapshot, str) or not re.fullmatch(HEX, snapshot):
                        raise Problem('Restic did not return an exact completed snapshot identifier.')
                    job.update(snapshotId=snapshot, phase='Restoring this exact snapshot to verify every file'); s._record(job)
                    s._command(self.repository, s.key_path, '--no-lock', 'restore', snapshot, '--target', str(temp / 'verification'))
                    verified = temp / 'verification/workspace'
                    s.verify_workspace(verified, s.closed)
                    if digest(verified / 'UNFORGE-RECOVERY.json', s.closed) != manifest_hash:
                        raise Problem('Restored workspace manifest differs from the captured workspace.')
                    # Flush the local writer before any destination publication.
                    sync_tree(self.repository)
                    descriptor = {'schema': 2, 'format': 'unforge-incremental-restic', 'pointId': job['id'],
                                  'writerId': writer, 'repositoryId': repository_id, 'snapshotId': snapshot,
                                  'createdAt': job['createdAt'], 'workspaceManifestSha256': manifest_hash,
                                  'sourceBytes': manifest['sourceBytes'], 'fileCount': manifest['fileCount'],
                                  'files': self._inventory(snapshot)}
                    for destination in destinations:
                        job['destinations'].append(self._publish(destination, descriptor)); s._record(job)
                    job.update(state='completed', phase='Incremental encrypted recovery point verified locally', finishedAt=stamp(),
                               files=manifest['fileCount'], bytes=manifest['sourceBytes'], excludedCount=len(manifest['excluded']),
                               restoredLocally=True, addedBytes=sum(d['addedBytes'] for d in job['destinations']),
                               reusedBytes=sum(d['reusedBytes'] for d in job['destinations']))
        except Exception as error:
            job.update(state='failed', error=str(error), finishedAt=stamp())
        s._record(job)
        return dict(job)

    def inspect(self, path, verify=True):
        point = Path(path).expanduser()
        if not point.is_absolute(): raise Problem('Choose an absolute recovery point path.')
        real_directory(point)
        if point.suffix != '.ufpoint' or point.parent.name != 'points' or point.parent.parent.suffix != '.ufvault':
            raise Problem('Keep the recovery point inside its original complete .ufvault folder.')
        vault = point.parent.parent; real_directory(vault)
        value = _json(point / 'POINT.json')
        if value.get('schema') != 2 or value.get('format') != 'unforge-incremental-restic': raise Problem('Unsupported recovery point format.')
        for name, pattern in [('pointId', r'[a-f0-9]{32}'), ('writerId', r'[a-f0-9]{32}'), ('repositoryId', HEX), ('snapshotId', HEX), ('workspaceManifestSha256', HEX), ('vaultIdentitySha256', HEX)]:
            if not isinstance(value.get(name), str) or not re.fullmatch(pattern, value[name]): raise Problem('Invalid recovery point identity.')
        if point.name != value['pointId'] + '.ufpoint': raise Problem('Recovery point identity does not match its folder.')
        if vault.name != 'unforge-' + value['repositoryId'] + '.ufvault': raise Problem('Recovery vault identity does not match its folder.')
        identity = _json(vault / 'VAULT.json')
        if identity != {'schema': 2, 'repositoryId': value['repositoryId'], 'writerId': value['writerId']} or digest(vault / 'VAULT.json', self.service.closed) != value['vaultIdentitySha256']:
            raise Problem('Recovery vault identity changed.')
        for field, bound in [('sourceBytes', MAX_BYTES), ('fileCount', MAX_FILES)]:
            if type(value.get(field)) is not int or not 0 <= value[field] <= bound: raise Problem('Invalid workspace recovery size.')
        entries = value.get('files')
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES: raise Problem('Invalid recovery object inventory.')
        seen = set(); total = 0
        for entry in entries:
            if not isinstance(entry, dict): raise Problem('Invalid recovery object.')
            relative = entry.get('path'); size = entry.get('bytes'); checksum = entry.get('sha256')
            if not isinstance(relative, str) or not (OBJECT.fullmatch(relative) or relative == 'snapshots/' + value['snapshotId']): raise Problem('Invalid encrypted object path.')
            if relative in seen or type(size) is not int or size < 0 or not isinstance(checksum, str) or not re.fullmatch(HEX, checksum): raise Problem('Invalid or duplicate encrypted object.')
            if relative != 'config' and relative.rsplit('/', 1)[-1] != checksum: raise Problem('Restic object identity differs from its checksum.')
            seen.add(relative); total += size
            if total > MAX_REPOSITORY_BYTES: raise Problem('Recovery vault exceeds the supported audit size.')
        if 'config' not in seen or 'snapshots/' + value['snapshotId'] not in seen or not any(name.startswith('keys/') for name in seen):
            raise Problem('Recovery point is missing its repository configuration, key or snapshot.')
        repository = vault / 'repository'; real_directory(repository)
        if verify:
            for entry in entries: self._validate_entry(repository, entry)
        required = [{**entry, 'path': 'repository/' + entry['path']} for entry in entries]
        required += [self._entry(vault / 'VAULT.json', 'VAULT.json'), self._entry(point / 'POINT.json', 'points/' + point.name + '/POINT.json')]
        return {**value, 'vaultPath': str(vault), 'repositoryPath': str(repository), 'requiredFiles': required, 'encryptedBytes': total}

    def _view(self, inspection, target):
        """Pin a point's object set so later snapshots/indexes cannot affect restore.

        Hard links reuse local bytes. Unsupported/cross-device links fall back to
        guarded copies. Restic reads this view with --no-lock and never writes it.
        """
        _mkdir(target)
        source = Path(inspection['repositoryPath'])
        for entry in inspection['files']:
            self._cancel()
            origin = source / entry['path']; output = target / entry['path']; _mkdir(output.parent)
            self._validate_entry(source, entry)
            try: os.link(origin, output, follow_symlinks=False)
            except OSError as error:
                if error.errno not in (errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP): raise
                storage_guard([(target, entry['bytes'] + STORAGE_METADATA_ALLOWANCE, 'recovery repository view')])
                self._install(origin, output, entry)
            self._validate_entry(target, entry)

    def restore(self, path, password, destination):
        s = self.service
        with s.lock:
            if s.worker and s.worker.is_alive(): raise Problem('Wait for the current backup operation.')
            inspection = self.inspect(path)
            target = Path(destination).expanduser()
            if not target.is_absolute(): raise Problem('Choose an absolute recovery folder.')
            if target.exists() or target.is_symlink(): raise Problem('Restore into a new folder; existing work is never overwritten.')
            # Validate existing ancestors before resolving, so links are rejected.
            ancestor = target.parent
            while not ancestor.exists():
                if ancestor.is_symlink(): raise Problem('Recovery folders cannot contain symbolic links.')
                ancestor = ancestor.parent
            real_directory(ancestor)
            if target.resolve() != target: raise Problem('Choose a recovery folder without symbolic links.')
            vault = Path(inspection['vaultPath'])
            if any(target.is_relative_to(root) or root.is_relative_to(target) for root in (s.engine.home, vault)):
                raise Problem('Recover outside the active workspace and encrypted vault.')
            if not isinstance(password, str) or not 1 <= len(password) <= 1024: raise Problem('Enter your recovery passphrase.')
            _mkdir(target.parent)
            with tempfile.TemporaryDirectory(prefix='.unforge-restore-', dir=target.parent) as tmp:
                temp = Path(tmp); key = temp / 'key'
                with key.open('x') as output: key.chmod(0o600); output.write(password)
                view = temp / 'repository'; self._view(inspection, view)
                config = json.loads(s._command(view, key, '--no-lock', 'cat', 'config'))
                if config.get('id') != inspection['repositoryId']: raise Problem('Encrypted repository identity does not match this point.')
                statistics = json.loads(s._command(view, key, '--no-lock', 'stats', inspection['snapshotId'], '--mode', 'restore-size'))
                size, count = statistics.get('total_size'), statistics.get('total_file_count')
                if type(size) is not int or type(count) is not int or not 0 <= size <= MAX_BYTES + MAX_FILES * 4096 + STORAGE_METADATA_ALLOWANCE or not 0 <= count <= MAX_FILES * 4 + 1:
                    raise Problem('Encrypted recovery size is unavailable or exceeds its supported limit.')
                storage_guard([(target.parent, size + count * 4096 + STORAGE_METADATA_ALLOWANCE, 'workspace recovery')])
                s._command(view, key, '--no-lock', 'restore', inspection['snapshotId'], '--target', str(temp / 'result'))
                workspace = temp / 'result/workspace'; manifest = s.verify_workspace(workspace, s.closed)
                if digest(workspace / 'UNFORGE-RECOVERY.json', s.closed) != inspection['workspaceManifestSha256'] or manifest.get('incrementalPoint') != {'pointId': inspection['pointId'], 'repositoryId': inspection['repositoryId']}:
                    raise Problem('Encrypted workspace does not belong to this selected recovery point.')
                if manifest['sourceBytes'] != inspection['sourceBytes'] or manifest['fileCount'] != inspection['fileCount']:
                    raise Problem('Recovery size does not match the encrypted workspace.')
                # Verify source and pinned view again after decryption/readback.
                for entry in inspection['files']: self._validate_entry(view, entry)
                after = self.inspect(path)
                if after != inspection: raise Problem('Recovery point changed during restore.')
                sync_tree(workspace); real_directory(target.parent)
                if target.exists() or target.is_symlink(): raise Problem('Recovery destination was created by another operation.')
                os.rename(workspace, target); sync_directory(target.parent)
            result = {'path': str(target), 'files': manifest['fileCount'], 'restoredAt': stamp(), 'state': 'restored',
                      'format': 'incremental-v2', 'snapshotId': inspection['snapshotId'], 'pointId': inspection['pointId'],
                      'applicationsStarted': False, 'externalData': manifest['externalData'], 'excluded': manifest['excluded']}
            atomic(s.root / ('restore-' + uuid.uuid4().hex + '.json'), result)
            return result
