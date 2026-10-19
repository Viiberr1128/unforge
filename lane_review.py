"""Bounded, read-only review packets for exact lane candidates."""
import hashlib
import json
from pathlib import Path

from engine import Problem
from lanes import claim_matches

MAX_CHANGED_PATHS = 1000
MAX_DIFF_BYTES = 256 * 1024
CONFIG_PATHS = ('*.json', '*.jsonc', '*.toml', '*.yaml', '*.yml')


def _bounded_text(raw, limit=MAX_DIFF_BYTES):
    truncated = len(raw) > limit
    return raw[:limit].decode('utf-8', errors='replace'), truncated


class LaneReview:
    def __init__(self, engine, lanes, checks, care, operating):
        self.engine = engine
        self.lanes = lanes
        self.checks = checks
        self.care = care
        self.operating = operating

    def _changed_paths(self, work, base, head):
        raw = self.engine.git(work, 'diff', '--name-status', '-z', '--no-renames', f'{base}...{head}', '--')
        fields = raw.split(b'\0')
        if fields and not fields[-1]:
            fields.pop()
        changed = []
        for index in range(0, len(fields) - 1, 2):
            status = fields[index].decode('ascii', errors='replace')
            path = fields[index + 1].decode('utf-8', errors='replace')
            changed.append({'status': status, 'path': path})
        return changed

    @staticmethod
    def _receipt_summary(receipt):
        if not receipt:
            return None
        return {
            'tree': receipt.get('tree'),
            'status': receipt.get('status'),
            'createdAt': receipt.get('createdAt'),
            'reused': receipt.get('reused', False),
            'error': receipt.get('error'),
            'jobs': [
                {key: job.get(key) for key in ('id', 'exitCode', 'timedOut', 'reused')}
                for job in (receipt.get('jobs') or [])
            ],
        }

    def _checks(self, pid, head):
        try:
            graph = self.checks.graph(pid, tree=head)
            if graph is None:
                return {'status': 'not-configured', 'graph': None, 'result': None, 'error': None}
            receipt = self.checks.result(pid, head, graph)
            return {
                'status': receipt.get('status') if receipt else 'not-checked',
                'graph': graph,
                'result': self._receipt_summary(receipt),
                'error': None,
            }
        except Problem as error:
            return {'status': 'invalid-configuration', 'graph': None, 'result': None, 'error': str(error)}

    def _production(self, pid, root, candidate):
        try:
            operating = self.operating.get(pid)
        except Problem as error:
            return {'ref': None, 'version': None, 'relation': None, 'error': str(error)}
        production = operating.get('productionVersion')
        result = {
            'ref': operating.get('productionRef'),
            'version': production,
            'relation': None,
            'error': operating.get('productionError'),
        }
        if not production:
            return result
        counts = self.engine.git(root, 'rev-list', '--left-right', '--count', f'{candidate}...{production}').decode().split()
        candidate_only, production_only = int(counts[0]), int(counts[1])
        if candidate_only == production_only == 0:
            kind = 'equal'
        elif candidate_only and not production_only:
            kind = 'ahead'
        elif production_only and not candidate_only:
            kind = 'behind'
        else:
            kind = 'diverged'
        result['relation'] = {
            'kind': kind,
            'candidateOnly': candidate_only,
            'productionOnly': production_only,
        }
        return result

    def review(self, pid, lid):
        with self.engine.lock:
            root = self.engine.root(pid)
            record = self.lanes._record(pid, lid)
            work = Path(record['path'])
            head = self.lanes._head(work)
            live = self.lanes._head(root)
            base = record['base']
            merge_base_result = self.engine.git_run(root, 'merge-base', live, head, allowed_returncodes=(0, 1))
            merge_base = merge_base_result.stdout.decode().strip() if merge_base_result.returncode == 0 else None
            changed = self._changed_paths(work, base, head)
            paths = [item['path'] for item in changed]
            claims = list(record.get('claimedPaths') or [])
            outside = [path for path in paths if claims and not any(claim_matches(claim, path) for claim in claims)]
            raw_diff = self.engine.git(work, 'diff', '--no-ext-diff', '--unified=3', f'{base}...{head}', '--')
            diff, diff_truncated = _bounded_text(raw_diff)
            checks = self._checks(pid, head)
            try:
                config_diff = self.engine.git(
                    work, 'diff', '--no-ext-diff', '--unified=3', f'{base}...{head}', '--', *CONFIG_PATHS)
                consequences = self.care.consequences(pid, config_diff.decode('utf-8', errors='replace'))
                consequence_error = None
            except (Problem, OSError, UnicodeError, ValueError) as error:
                consequences, consequence_error = None, str(error)[:1000]
            production = self._production(pid, root, head)
            records = self.lanes._load(pid)
            claim_conflicts = self.lanes._claim_conflicts(record, records)
            parent = next((item for item in records if item.get('id') == record.get('parent')), None)
            parent_pending = bool(parent and parent.get('status') != 'merged')
            needs_restack = not parent_pending and live != base
            changed_public = changed[:MAX_CHANGED_PATHS]
            outside_public = outside[:MAX_CHANGED_PATHS]
            stable = {
                'laneId': lid,
                'base': base,
                'head': head,
                'liveHead': live,
                'mergeBase': merge_base,
                'claimedPaths': claims,
                'changedPaths': changed,
                'outOfClaimPaths': outside,
                'claimConflicts': claim_conflicts,
                'diffSha256': hashlib.sha256(raw_diff).hexdigest(),
                'checks': checks,
                'production': production,
                'parent': {'id': parent.get('id'), 'name': parent.get('name'), 'status': parent.get('status')} if parent else None,
                'parentPending': parent_pending,
                'needsRestack': needs_restack,
            }
            review_digest = hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            return {
                'laneId': lid,
                'name': record.get('name'),
                'incomingTitle': record.get('incomingTitle'),
                'incomingNote': record.get('incomingNote'),
                'status': record.get('status'),
                'dirty': bool(self.engine.git(work, 'status', '--porcelain')),
                'base': base,
                'head': head,
                'liveHead': live,
                'mergeBase': merge_base,
                'reviewDigest': review_digest,
                'parent': stable['parent'],
                'parentPending': parent_pending,
                'needsRestack': needs_restack,
                'claimedPaths': claims,
                'claimConflicts': claim_conflicts,
                'changedPaths': changed_public,
                'changedPathCount': len(changed),
                'changedPathsTruncated': len(changed) > len(changed_public),
                'outOfClaimPaths': outside_public,
                'outOfClaimPathCount': len(outside),
                'outOfClaimPathsTruncated': len(outside) > len(outside_public),
                'diff': diff,
                'diffTruncated': diff_truncated,
                'diffSha256': stable['diffSha256'],
                'checks': checks,
                'consequences': consequences,
                'consequenceError': consequence_error,
                'production': production,
            }
