"""Send a lane to another person as a file. No Unforge account or GitHub."""
import gzip
import io
import json
import os
import re
import stat
from pathlib import Path
import tarfile
import tempfile
import uuid

from engine import MAX_TEXT, Problem
from lanes import BRANCH_PREFIX, MAX_LANES, Lanes, now

LIMIT = 20 * 1024 * 1024


class Exchange:
    def __init__(self, engine, lanes=None):
        self.engine = engine
        self.lanes = lanes or Lanes(engine)

    def export_lane(self, pid, lane_id, title, note, output_path):
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            raise Problem('Give the change a short title')
        if not isinstance(note, str) or len(note) > MAX_TEXT:
            raise Problem('The note is too long')
        if not isinstance(output_path, str) or not Path(output_path).is_absolute():
            raise Problem('Choose an absolute file path for the change')
        destination = Path(output_path)
        if destination.exists() or destination.is_symlink():
            raise Problem('Choose a new file path. Existing files are never overwritten.')
        with self.engine.lock:
            lane = self.lanes.get(pid, lane_id)
            if lane.get('dirty'):
                raise Problem('Save the lane before sending it')
            if lane.get('head') == lane.get('base'):
                raise Problem('This lane has no saved change to send')
            work = Path(lane['path'])
            with tempfile.TemporaryDirectory(prefix='unforge-exchange-') as temporary:
                bundle = Path(temporary) / 'change.bundle'
                exported_ref = 'refs/unforge-exchange/' + uuid.uuid4().hex
                self.engine.git(work, 'update-ref', exported_ref, lane['head'])
                try:
                    self.engine.git(work, 'bundle', 'create', str(bundle), lane['base'] + '..' + exported_ref)
                finally:
                    self.engine.git(work, 'update-ref', '-d', exported_ref, lane['head'])
                if not bundle.is_file() or bundle.stat().st_size > LIMIT:
                    raise Problem('This change is larger than 20 MiB. Send the project first, then a smaller lane.')
                manifest = {
                    'schemaVersion': 1,
                    'kind': 'change',
                    'title': title.strip(),
                    'note': (note or '').strip(),
                    'laneName': lane['name'],
                    'base': lane['base'],
                    'head': lane['head'],
                    'createdAt': now(),
                }
                archive = Path(temporary) / 'handoff.unforge-change'
                with tarfile.open(archive, 'w:gz') as tar:
                    tar.add(bundle, arcname='change.bundle')
                    info = tarfile.TarInfo('manifest.json')
                    payload = json.dumps(manifest, indent=2).encode()
                    info.size = len(payload)
                    tar.addfile(info, fileobj=io.BytesIO(payload))
                if archive.stat().st_size > LIMIT:
                    raise Problem('This compressed change exceeds 20 MiB. Send a smaller lane.')
                destination.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    with os.fdopen(descriptor, 'wb') as output:
                        output.write(archive.read_bytes())
                        output.flush()
                        os.fsync(output.fileno())
                except BaseException:
                    destination.unlink(missing_ok=True)
                    raise
            return {'path': str(destination), 'bytes': destination.stat().st_size, 'manifest': manifest}

    def _read_archive(self, source, folder):
        """Read only the two bounded regular members; never extract an archive."""
        limits = {'manifest.json': MAX_TEXT, 'change.bundle': LIMIT}
        seen = set()
        try:
            # Bound gzip expansion before tarfile parses extended headers or
            # member metadata; those are outside a member-size check.
            expanded = folder / 'bounded.tar'
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as compressed:
                info = os.fstat(compressed.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
                    raise Problem('Change file must be a bounded regular file')
                with gzip.GzipFile(fileobj=compressed) as stream, expanded.open('xb') as output:
                    remaining = LIMIT + MAX_TEXT + 65536
                    while True:
                        chunk = stream.read(min(65536, remaining + 1))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise Problem('Change archive exceeds its expanded size limit')
                        output.write(chunk)
            with tarfile.open(expanded, 'r:') as archive:
                for member in archive:
                    if member.name not in limits or member.name in seen or not member.isfile():
                        raise Problem('Change archives contain only one manifest.json and one regular change.bundle')
                    if member.size < 1 or member.size > limits[member.name]:
                        raise Problem('Change archive member exceeds its supported size limit')
                    seen.add(member.name)
                    with archive.extractfile(member) as reader, (folder / member.name).open('xb') as output:
                        remaining = member.size
                        while remaining:
                            chunk = reader.read(min(65536, remaining))
                            if not chunk:
                                raise Problem('Incomplete change archive member')
                            output.write(chunk)
                            remaining -= len(chunk)
            if seen != set(limits):
                raise Problem('Change archive is missing its manifest or Git bundle')
            manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        except (tarfile.TarError, OSError, EOFError, UnicodeError, ValueError) as error:
            raise Problem('Cannot read this bounded Unforge change archive: ' + str(error)) from error
        if (not isinstance(manifest, dict) or manifest.get('schemaVersion') != 1 or manifest.get('kind') != 'change' or
                any(not isinstance(manifest.get(key), str) or not re.fullmatch(r'[a-f0-9]{40,64}', manifest[key])
                    for key in ('base', 'head'))):
            raise Problem('Change manifest must name its exact base and proposed Git commits')
        for key, maximum in (('title', 120), ('note', MAX_TEXT), ('laneName', 80)):
            if not isinstance(manifest.get(key), str) or len(manifest[key]) > maximum:
                raise Problem('Change manifest contains invalid text')
        return manifest

    def import_change(self, pid, path):
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise Problem('Choose an absolute path to the change file')
        source = Path(path)
        if source.is_symlink() or not source.is_file() or source.stat().st_size > LIMIT:
            raise Problem('Change file must be a regular file no larger than 20 MiB')
        with self.engine.lock:
            root = self.engine.root(pid)
            if self.engine.git(root, 'status', '--porcelain'):
                raise Problem('Save your current changes before importing a change')
            live = self.lanes._head(root)
            records = self.lanes._load(pid)
            if len([item for item in records if item.get('status') in ('open', 'needs_restack', 'conflict')]) >= MAX_LANES:
                raise Problem('This project already has 32 active lanes. Merge or close one first.')
            with tempfile.TemporaryDirectory(prefix='unforge-exchange-in-') as temporary:
                folder = Path(temporary)
                manifest = self._read_archive(source, folder)
                bundle = folder / 'change.bundle'
                # Verify and inspect in an independent repository before adding
                # any refs or writing a checkout in the user's project.
                probe = folder / 'probe'
                self.engine.git(root, 'clone', '--no-hardlinks', '--no-checkout', '--template=', '--', str(root), str(probe))
                verify = self.engine.git_run(probe, 'bundle', 'verify', str(bundle), allowed_returncodes=(0, 1, 128))
                if verify.returncode != 0:
                    raise Problem('This change needs the same project history. Import the project first, then the change.')
                heads = self.engine.git(probe, 'bundle', 'list-heads', str(bundle)).decode().splitlines()
                exported = [line.split(' ', 1)[1] for line in heads if line.startswith(manifest['head'] + ' ')]
                if not exported:
                    raise Problem('The change bundle does not match its manifest head')
                self.engine.git(probe, 'fetch', '--no-tags', str(bundle), manifest['head'] + ':refs/heads/unforge-incoming')
                if self.engine.git(probe, 'rev-parse', 'refs/heads/unforge-incoming').decode().strip() != manifest['head']:
                    raise Problem('The imported Git commit does not match its manifest')
                self.engine.git(probe, 'merge-base', '--is-ancestor', manifest['base'], manifest['head'])
                self.engine.validate_checkout_size(probe, manifest['head'])
                self.engine.validate_tree(probe, manifest['head'])
                if self.lanes._head(root) != live or self.engine.git(root, 'status', '--porcelain'):
                    raise Problem('Your project changed during import. Reload before importing the change.')
                lid = uuid.uuid4().hex
                branch = BRANCH_PREFIX + lid
                work = self.lanes._work(pid, lid)
                work.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    self.engine.git(root, 'fetch', '--no-tags', str(probe), manifest['head'] + ':refs/heads/' + branch)
                    self.engine.git(root, 'worktree', 'add', str(work), branch)
                    record = dict(
                        id=lid, name=(manifest['laneName'] or manifest['title'] or 'Incoming change')[:80],
                        status='open', parent=None, base=manifest['base'],
                        head=self.lanes._head(work), branch=branch, path=str(work), claimedPaths=[],
                        changedFiles=[], dirty=False, createdAt=now(), updatedAt=now(), conflictFiles=[],
                        incomingTitle=manifest['title'], incomingNote=manifest['note'],
                    )
                    self.lanes._replace(pid, record)
                    return self.lanes._public(pid, record)
                except Exception:
                    if work.exists():
                        self.engine.git(root, 'worktree', 'remove', '--force', str(work), allowed_returncodes=(0, 128))
                    self.engine.git(root, 'update-ref', '-d', 'refs/heads/' + branch, manifest['head'], allowed_returncodes=(0, 1))
                    raise
