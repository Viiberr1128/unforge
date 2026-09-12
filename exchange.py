"""Send a lane to another person as a file. No Unforge account or GitHub."""
import io
import json
from pathlib import Path
import tarfile
import tempfile
import uuid

from engine import MAX_TEXT, Problem
from lanes import BRANCH_PREFIX, Lanes, now

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
                self.engine.git(work, 'bundle', 'create', str(bundle), lane['base'] + '..HEAD')
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
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read_bytes())
                destination.chmod(0o600)
            return {'path': str(destination), 'bytes': destination.stat().st_size, 'manifest': manifest}

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
            with tempfile.TemporaryDirectory(prefix='unforge-exchange-in-') as temporary:
                try:
                    with tarfile.open(source, 'r:gz') as tar:
                        tar.extractall(temporary, filter='data')
                except (tarfile.TarError, TypeError):
                    with tarfile.open(source, 'r:gz') as tar:
                        tar.extractall(temporary)
                folder = Path(temporary)
                manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
                bundle = folder / 'change.bundle'
                if manifest.get('kind') != 'change' or not bundle.is_file():
                    raise Problem('That file is not an Unforge change')
                verify = self.engine.git_run(root, 'bundle', 'verify', str(bundle), allowed_returncodes=(0, 1, 128))
                if verify.returncode != 0:
                    raise Problem('This change needs the same project history. Import the project on this Mac first, then the change.')
                lid = uuid.uuid4().hex
                branch = BRANCH_PREFIX + lid
                self.engine.git(root, 'fetch', str(bundle), f'HEAD:{branch}')
                work = self.lanes._work(pid, lid)
                work.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.engine.git(root, 'worktree', 'add', str(work), branch)
                record = dict(
                    id=lid, name=str(manifest.get('laneName') or manifest.get('title') or 'Incoming change')[:80],
                    status='open', parent=None, base=manifest.get('base') or self.lanes._head(root),
                    head=self.lanes._head(work), branch=branch, path=str(work), claimedPaths=[],
                    changedFiles=[], dirty=False, createdAt=now(), updatedAt=now(), conflictFiles=[],
                    incomingTitle=manifest.get('title'), incomingNote=manifest.get('note'),
                )
                self.lanes._replace(pid, record)
                return self.lanes._public(pid, record)
