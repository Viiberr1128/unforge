"""Merge immutable lane commits and retain real Git restack conflicts for review."""
import hashlib
import json
from pathlib import Path

from engine import MAX_TEXT, Problem, UNSET
from lanes import Lanes


class Integrate:
    def __init__(self, engine, lanes=None):
        self.engine = engine
        self.lanes = lanes or Lanes(engine)

    def _files_between(self, repo, base, head):
        raw = self.engine.git(repo, 'diff', '--name-only', '-z', f'{base}...{head}').decode()
        return [item for item in raw.split('\0') if item]

    def _merge_head(self, work):
        return self.engine.git(work, 'rev-parse', '--verify', 'MERGE_HEAD', allowed_returncodes=(0, 128)).decode().strip()

    def _unmerged(self, work):
        return [path for path in self.engine.git(work, 'diff', '--name-only', '--diff-filter=U', '-z').decode().split('\0') if path]

    def overlap(self, pid, lid):
        root = self.engine.root(pid)
        record = self.lanes._record(pid, lid)
        live = self.lanes._head(root)
        if live == record['base']:
            return []
        work = Path(record['path'])
        lane_files = set(self._files_between(work, record['base'], self.lanes._head(work)))
        live_files = set(self._files_between(root, record['base'], live))
        return sorted(lane_files & live_files)

    def _assert_conflict(self, record, work):
        if (not record.get('restackBase') or self._merge_head(work) != record['restackBase'] or
                self.lanes._head(work) != record.get('restackHead')):
            raise Problem('This conflict changed outside Unforge. Finish or abort the Git merge in the lane, then reload.')

    def _finish_restack(self, pid, lid, record, work):
        self._assert_conflict(record, work)
        self.engine.git(work, 'commit', '--no-edit')
        return self.lanes.mark(pid, lid, status='open', base=record['restackBase'],
                               head=self.lanes._head(work), conflictFiles=[], restackBase=None, restackHead=None)

    def restack(self, pid, lid):
        with self.engine.lock:
            root = self.engine.root(pid)
            record = self.lanes._record(pid, lid)
            if record.get('status') not in ('open', 'needs_restack', 'conflict'):
                raise Problem('This lane cannot be restacked')
            work = Path(record['path'])
            if self._merge_head(work):
                self._assert_conflict(record, work)
                if not self._unmerged(work):
                    return self._finish_restack(pid, lid, record, work)
                return self.lanes.mark(pid, lid, status='conflict', conflictFiles=self._unmerged(work))
            if self.engine.git(work, 'status', '--porcelain'):
                raise Problem('Save the lane before restacking it')
            parent_id = record.get('parent')
            if parent_id:
                parent = self.lanes._record(pid, parent_id)
                if parent.get('status') != 'merged':
                    parent_work = Path(parent['path'])
                    if self.engine.git(parent_work, 'status', '--porcelain'):
                        raise Problem('Save the parent lane before restacking its child')
                    new_base = self.lanes._head(parent_work)
                else:
                    new_base = self.lanes._head(root)
            else:
                new_base = self.lanes._head(root)
            if new_base == record['base']:
                return self.lanes.mark(pid, lid, status='open', head=self.lanes._head(work), conflictFiles=[])
            old_head = self.lanes._head(work)
            merged = self.engine.git_run(work, 'merge', '--no-edit', new_base, allowed_returncodes=(0, 1))
            if merged.returncode:
                files = self._unmerged(work)
                if not files or self._merge_head(work) != new_base:
                    raise Problem(merged.stderr.decode(errors='replace').strip() or 'The lane could not be restacked')
                # Keep Git's index stages and merge state; ordinary Git can also
                # finish this operation outside Unforge.
                return self.lanes.mark(pid, lid, status='conflict', conflictFiles=files,
                                       head=old_head, restackHead=old_head, restackBase=new_base)
            return self.lanes.mark(pid, lid, status='open', base=new_base, conflictFiles=[],
                                   head=self.lanes._head(work), restackHead=None, restackBase=None)

    def conflicts(self, pid, lid):
        with self.engine.lock:
            self.engine.root(pid)
            record = self.lanes._record(pid, lid)
            work = Path(record['path'])
            self._assert_conflict(record, work)
            files = []
            unresolved = self._unmerged(work)
            # Review a bounded batch; after resolution the next batch becomes
            # available. Do not load thousands of full conflict blobs at once.
            for path in unresolved[:8]:
                target = self.engine.safe_path(work, path)
                item = {'path': path, 'readable': True}
                for key, stage in (('base', 1), ('ours', 2), ('theirs', 3)):
                    spec = f':{stage}:{path}'
                    size = self.engine.git_run(work, 'cat-file', '-s', spec, allowed_returncodes=(0, 128))
                    if size.returncode == 0 and int(size.stdout) > MAX_TEXT:
                        item['readable'] = False
                        item[key] = None
                        continue
                    value = self.engine.git_run(work, 'show', spec, allowed_returncodes=(0, 128))
                    raw = value.stdout if value.returncode == 0 else None
                    if raw is not None and (len(raw) > MAX_TEXT or b'\0' in raw):
                        item['readable'] = False
                        item[key] = None
                    else:
                        try:
                            item[key] = raw.decode('utf-8') if raw is not None else None
                        except UnicodeError:
                            item[key] = None
                            item['readable'] = False
                if target.exists():
                    if not target.is_file() or target.stat().st_size > MAX_TEXT:
                        item['readable'] = False
                        item['current'] = None
                    else:
                        raw = target.read_bytes()
                        item['currentHash'] = hashlib.sha256(raw).hexdigest()
                        try:
                            item['current'] = raw.decode('utf-8') if b'\0' not in raw else None
                            item['readable'] &= item['current'] is not None
                        except UnicodeError:
                            item['current'] = None
                            item['readable'] = False
                else:
                    item['current'] = None
                files.append(item)
            document = {'head': record['restackHead'], 'base': record['restackBase'], 'files': files, 'total': len(unresolved)}
            document['revision'] = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
            return document

    def resolve(self, pid, lid, path, strategy, content=None, expected_content=UNSET, expected_revision=None):
        if strategy not in ('ours', 'theirs', 'edit'):
            raise Problem('Choose ours, theirs, or edit')
        with self.engine.lock:
            record = self.lanes._record(pid, lid)
            work = Path(record['path'])
            self._assert_conflict(record, work)
            if path not in self._unmerged(work):
                raise Problem('Choose a current unresolved conflict')
            if expected_revision is not None and self.conflicts(pid, lid)['revision'] != expected_revision:
                raise Problem('This conflict changed. Reload its choices before resolving it.')
            target = self.engine.safe_path(work, path)
            if strategy == 'edit':
                self.lanes.write(pid, lid, path, content, expected_content)
                self.engine.git(work, 'add', '--', path)
            else:
                stage = '2' if strategy == 'ours' else '3'
                exists = self.engine.git_run(work, 'cat-file', '-e', f':{stage}:{path}', allowed_returncodes=(0, 128)).returncode == 0
                if exists:
                    self.engine.git(work, 'checkout', '--ours' if strategy == 'ours' else '--theirs', '--', path)
                    self.engine.git(work, 'add', '--', path)
                else:
                    self.engine.git(work, 'rm', '-f', '--', path)
            remaining = self._unmerged(work)
            if remaining:
                return self.lanes.mark(pid, lid, conflictFiles=remaining, status='conflict')
            return self._finish_restack(pid, lid, record, work)

    def merge(self, pid, lid, *, require_checks=None, expected_head=None, expected_live=None):
        with self.engine.lock:
            root = self.engine.root(pid)
            if self.engine.git(root, 'status', '--porcelain'):
                raise Problem('Save your current changes before merging a lane')
            record = self.lanes._record(pid, lid)
            if record.get('status') in ('merged', 'closed'):
                raise Problem('This lane has already been merged or closed')
            work = Path(record['path'])
            if self._merge_head(work) or record.get('status') == 'conflict':
                raise Problem('Resolve restack conflicts before merging')
            parent_id = record.get('parent')
            if parent_id and self.lanes._record(pid, parent_id).get('status') != 'merged':
                raise Problem('Merge the parent lane first, then restack this one')
            if self.engine.git(work, 'status', '--porcelain'):
                raise Problem('Save the lane before merging it')
            live = self.lanes._head(root)
            current_head = self.lanes._head(work)
            if expected_head is not None and expected_head != current_head:
                raise Problem('This lane changed after review. Review the current candidate before merging.')
            if expected_live is not None and expected_live != live:
                raise Problem('Working source changed after review. Review this lane against the current source.')
            if live != record['base']:
                if expected_head is not None or expected_live is not None:
                    raise Problem('Working source moved since this lane began. Restack the lane, then review it again.')
                overlapped = self.overlap(pid, lid)
                if overlapped:
                    self.lanes.mark(pid, lid, status='needs_restack', conflictFiles=overlapped)
                    raise Problem('Live source moved in files this lane also changed. Restack it before merging: '
                                  + ', '.join(overlapped[:8]))
                self.restack(pid, lid)
                record = self.lanes._record(pid, lid)
                if record.get('status') == 'conflict':
                    raise Problem('Resolve restack conflicts before merging')
                work = Path(record['path'])
            candidate = self.lanes._head(work)
            self.engine.validate_checkout_size(work, candidate)
            self.engine.validate_tree(work, candidate)
            captured = {
                'base': record.get('base'),
                'candidate': candidate,
                'live': self.lanes._head(root),
                'parent': record.get('parent'),
                'status': record.get('status'),
            }

        if require_checks is not None and not require_checks(candidate):
            raise Problem('Checks must pass on this lane before it can merge')

        with self.engine.lock:
            root = self.engine.root(pid)
            record = self.lanes._record(pid, lid)
            work = Path(record['path'])
            # External Git tools do not acquire the Python lock. Never trust a
            # mutable branch name or the persisted lane head after a check.
            if (record.get('base') != captured['base'] or record.get('parent') != captured['parent'] or
                    record.get('status') != captured['status'] or self._merge_head(work) or
                    self.lanes._head(work) != captured['candidate'] or self.lanes._head(root) != captured['live'] or
                    self.engine.git(work, 'status', '--porcelain') or self.engine.git(root, 'status', '--porcelain')):
                raise Problem('Source changed while checks ran. Reload and check the current lane before merging.')
            collisions = []
            for path in self._files_between(work, record['base'], captured['candidate']):
                target = root / path
                tracked = bool(self.engine.git(root, 'ls-files', '-z', '--', path).strip())
                ignored = self.engine.git_run(root, 'check-ignore', '-q', '--', path, allowed_returncodes=(0, 1)).returncode == 0
                if (target.exists() or ignored) and not tracked:
                    collisions.append(path)
            if collisions:
                raise Problem('Merging this lane would overwrite an untracked or ignored file.')
            merged = self.engine.git_run(root, 'merge', '--no-ff', '-m', 'Merge lane: ' + record['name'], captured['candidate'],
                                         allowed_returncodes=(0, 1))
            if merged.returncode:
                self.engine.git(root, 'merge', '--abort', allowed_returncodes=(0, 128))
                raise Problem(merged.stderr.decode(errors='replace').strip() or 'The lane could not be merged')
            self.lanes.mark(pid, lid, status='merged', head=captured['candidate'])
            for child in self.lanes.children(pid, lid):
                if child.get('status') in ('open', 'needs_restack'):
                    self.lanes.mark(pid, child['id'], status='needs_restack')
            return self.engine.detail(pid)
