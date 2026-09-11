"""Merge and restack lanes onto live source. Never silent-overwrite a moved HEAD."""
from pathlib import Path

from engine import Problem
from lanes import Lanes


class Integrate:
    def __init__(self, engine, lanes=None):
        self.engine = engine
        self.lanes = lanes or Lanes(engine)

    def _files_between(self, repo, base, head):
        raw = self.engine.git(repo, 'diff', '--name-only', '-z', f'{base}...{head}').decode()
        return [item for item in raw.split('\0') if item]

    def overlap(self, pid, lid):
        root = self.engine.root(pid)
        record = self.lanes._record(pid, lid)
        live = self.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
        if live == record['base']:
            return []
        work = Path(record['path'])
        lane_files = set(self._files_between(work, record['base'], record['head']))
        live_files = set(self._files_between(root, record['base'], live))
        return sorted(lane_files & live_files)

    def restack(self, pid, lid):
        with self.engine.lock:
            root = self.engine.root(pid)
            record = self.lanes._record(pid, lid)
            if record.get('status') not in ('open', 'needs_restack', 'conflict'):
                raise Problem('This lane cannot be restacked')
            work = Path(record['path'])
            if self.engine.git(work, 'status', '--porcelain'):
                raise Problem('Save the lane before restacking it')
            parent_id = record.get('parent')
            if parent_id:
                parent = self.lanes._record(pid, parent_id)
                if parent.get('status') != 'merged':
                    new_base = parent['head']
                else:
                    new_base = self.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
            else:
                new_base = self.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
            if new_base == record['base']:
                return self.lanes.get(pid, lid)
            merged = self.engine.git_run(work, 'merge', '--no-edit', new_base, allowed_returncodes=(0, 1))
            if merged.returncode:
                unmerged = self.engine.git(work, 'diff', '--name-only', '--diff-filter=U', '-z').decode()
                files = [item for item in unmerged.split('\0') if item]
                self.engine.git(work, 'merge', '--abort', allowed_returncodes=(0, 128))
                self.lanes.mark(pid, lid, status='conflict', conflictFiles=files)
                raise Problem(
                    'This lane overlaps files that already landed. Restack it and choose what to keep in: '
                    + ', '.join(files[:8] or ['conflicting files']))
            self.lanes.mark(pid, lid, status='open', base=new_base, conflictFiles=[],
                            head=self.engine.git(work, 'rev-parse', 'HEAD').decode().strip())
            return self.lanes.get(pid, lid)

    def resolve(self, pid, lid, path, strategy, content=None):
        if strategy not in ('ours', 'theirs', 'edit'):
            raise Problem('Choose ours, theirs, or edit')
        with self.engine.lock:
            record = self.lanes._record(pid, lid)
            work = Path(record['path'])
            target = self.engine.safe_path(work, path)
            if strategy == 'edit':
                if not isinstance(content, str):
                    raise Problem('Edited conflict resolution needs file text')
                target.write_text(content, encoding='utf-8')
            else:
                stage = '--ours' if strategy == 'ours' else '--theirs'
                self.engine.git(work, 'checkout', stage, '--', path)
            self.engine.git(work, 'add', '--', path)
            remaining = self.engine.git(work, 'diff', '--name-only', '--diff-filter=U', '-z').decode()
            files = [item for item in remaining.split('\0') if item]
            self.lanes.mark(pid, lid, conflictFiles=files, status='conflict' if files else 'open')
            return self.lanes.get(pid, lid)

    def merge(self, pid, lid, *, require_checks=None):
        with self.engine.lock:
            root = self.engine.root(pid)
            if self.engine.git(root, 'status', '--porcelain'):
                raise Problem('Save your current changes before merging a lane')
            record = self.lanes._record(pid, lid)
            if record.get('status') in ('merged', 'closed'):
                raise Problem('This lane has already been merged or closed')
            if record.get('status') == 'conflict':
                raise Problem('Resolve restack conflicts before merging')
            parent_id = record.get('parent')
            if parent_id:
                parent = self.lanes._record(pid, parent_id)
                if parent.get('status') != 'merged':
                    raise Problem('Merge the parent lane first, then restack this one')
            work = Path(record['path'])
            if self.engine.git(work, 'status', '--porcelain'):
                raise Problem('Save the lane before merging it')
            live = self.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
            if live != record['base']:
                overlapped = self.overlap(pid, lid)
                if overlapped:
                    self.lanes.mark(pid, lid, status='needs_restack', conflictFiles=overlapped)
                    raise Problem(
                        'Live source moved in files this lane also changed. Restack it before merging: '
                        + ', '.join(overlapped[:8]))
                self.restack(pid, lid)
                record = self.lanes._record(pid, lid)
            if require_checks is not None and not require_checks(record['head']):
                raise Problem('Checks must pass on this lane before it can merge')
            collisions = []
            for path in self.lanes.changed_files(pid, lid):
                target = root / path
                tracked = bool(self.engine.git(root, 'ls-files', '-z', '--', path).strip())
                ignored = self.engine.git_run(root, 'check-ignore', '-q', '--', path, allowed_returncodes=(0, 1)).returncode == 0
                if target.exists() and not tracked:
                    collisions.append(path)
                elif ignored and not tracked:
                    collisions.append(path)
            if collisions:
                raise Problem('Merging this lane would overwrite an untracked or ignored file.')
            message = 'Merge lane: ' + record['name']
            merged = self.engine.git_run(root, 'merge', '--no-ff', '-m', message, record['branch'],
                                         allowed_returncodes=(0, 1))
            if merged.returncode:
                self.engine.git(root, 'merge', '--abort', allowed_returncodes=(0, 128))
                raise Problem(merged.stderr.decode(errors='replace').strip() or 'The lane could not be merged')
            self.lanes.mark(pid, lid, status='merged',
                            head=self.engine.git(root, 'rev-parse', 'HEAD').decode().strip())
            for child in self.lanes.children(pid, lid):
                if child.get('status') in ('open', 'needs_restack'):
                    self.lanes.mark(pid, child['id'], status='needs_restack')
            return self.engine.detail(pid)
