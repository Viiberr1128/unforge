"""Read ordinary Git state, without changing checkouts or invoking providers."""
import json
from pathlib import Path
from engine import MAX_TEXT, Problem

class Operating:
    def __init__(self, engine):
        self.engine = engine

    def get(self, pid):
        with self.engine.lock:
            root = self.engine.root(pid)
            config_path = self.engine.safe_path(root, '.unforge/app.json')
            config = {}
            if config_path.is_file():
                with config_path.open('rb') as file:
                    raw = file.read(MAX_TEXT + 1)
                if len(raw) > MAX_TEXT:
                    raise Problem('Application settings exceed their size limit')
                try:
                    config = json.loads(raw)
                except ValueError as error:
                    raise Problem('Application settings need repair') from error
                if not isinstance(config, dict):
                    raise Problem('Application settings need repair')
            head = self.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
            branch = self.engine.git(root, 'symbolic-ref', '--quiet', '--short', 'HEAD', allowed_returncodes=(0, 1)).decode().strip() or 'detached'
            dirty = bool(self.engine.git(root, 'status', '--porcelain'))
            refs = self.engine.git(root, 'for-each-ref', '--format=%(refname)', 'refs/').decode().splitlines()
            candidates = [ref for ref in refs if ref.endswith('/production')][:20]
            production_ref = config.get('productionRef') or (candidates[0] if len(candidates) == 1 else None)
            production, divergence, production_error = None, None, None
            if production_ref:
                if not isinstance(production_ref, str) or not production_ref.startswith('refs/') or len(production_ref) > 240:
                    production_error = 'Choose a full production ref beginning with refs/'
                else:
                    valid = self.engine.git_run(root, 'check-ref-format', production_ref, allowed_returncodes=(0, 1))
                    resolved = self.engine.git_run(root, 'rev-parse', '--verify', production_ref + '^{commit}', allowed_returncodes=(0, 128)) if valid.returncode == 0 else None
                    if resolved is None or resolved.returncode:
                        production_error = 'Production ref is unavailable'
                    else:
                        production = resolved.stdout.decode().strip()
                        counts = self.engine.git(root, 'rev-list', '--left-right', '--count', head + '...' + production).decode().split()
                        divergence = {'workingOnly': int(counts[0]), 'productionOnly': int(counts[1])}
            worktrees = []
            for block in self.engine.git(root, 'worktree', 'list', '--porcelain').decode().strip().split('\n\n')[:100]:
                record = {}
                for line in block.splitlines():
                    key, _, value = line.partition(' ')
                    if key in ('worktree', 'HEAD', 'branch', 'prunable', 'locked'):
                        record[key] = value
                if record.get('worktree'):
                    worktrees.append({'path': record['worktree'], 'head': record.get('HEAD'), 'branch': record.get('branch'), 'present': Path(record['worktree']).is_dir(), 'prunable': 'prunable' in record, 'locked': 'locked' in record})
            return {'sourceVersion': head, 'branch': branch, 'sourceDirty': dirty, 'productionRef': production_ref, 'productionVersion': production, 'productionRefCandidates': candidates, 'productionError': production_error, 'divergence': divergence, 'worktrees': worktrees, 'category': config.get('category', 'project')}
