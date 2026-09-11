"""Bound destinations and observed live identity. Merge is not done until the site matches."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

from engine import Problem
from lanes import atomic_json


def now():
    return datetime.now(timezone.utc).isoformat()


class Releases:
    def __init__(self, engine, checks=None):
        self.engine = engine
        self.checks = checks
        self.root = engine.home / '.releases'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def document(self, pid):
        path = self.engine.root(pid) / '.unforge' / 'app.json'
        if not path.is_file():
            return {'schemaVersion': 1, 'destinations': [], 'githubAbsent': False, 'blockers': [
                'Bind a destination and observe a live publish before claiming GitHub is gone.']}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError, UnicodeError) as error:
            raise Problem('App settings are damaged.') from error
        if not isinstance(data, dict):
            raise Problem('App settings must be an object')
        return data

    def destinations(self, pid):
        data = self.document(pid)
        items = data.get('destinations') or []
        if not isinstance(items, list):
            raise Problem('Destinations must be a list')
        return items

    def _state_path(self, pid):
        return self.root / (pid + '.json')

    def history(self, pid):
        path = self._state_path(pid)
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError, UnicodeError):
            return []
        return data if isinstance(data, list) else []

    def last_observed(self, pid, destination_id=None):
        for item in reversed(self.history(pid)):
            if item.get('observed') and (destination_id is None or item.get('destinationId') == destination_id):
                return item
        return None

    def publish(self, pid, destination_id, *, tree=None):
        with self.engine.lock:
            root = self.engine.root(pid)
            if self.engine.git(root, 'status', '--porcelain'):
                raise Problem('Save a version before publishing')
            tree = tree or self.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
            if self.checks and not self.checks.passed(pid, tree):
                raise Problem('Checks must pass on this version before it can go live')
            dest = next((item for item in self.destinations(pid) if item.get('id') == destination_id), None)
            if not dest:
                raise Problem('Unknown destination')
            kind = dest.get('type')
            artifact = hashlib.sha256(tree.encode()).hexdigest()
            observed = None
            if kind == 'local':
                target = dest.get('path')
                if not isinstance(target, str) or not target:
                    raise Problem('Local destination needs a folder path')
                folder = Path(target)
                if not folder.is_absolute():
                    folder = root / target
                folder.mkdir(parents=True, exist_ok=True)
                marker = folder / 'UNFORGE_RELEASE'
                marker.write_text(tree + '\n', encoding='utf-8')
                observed = self._observe_local(folder, tree)
            elif kind == 'http':
                url = dest.get('url')
                if not isinstance(url, str) or not url.startswith(('http://127.0.0.1', 'https://')):
                    raise Problem('HTTP destinations must be local or https')
                # Caller must have already placed the artifact. Observation proves it.
                observed = self._observe_http(url, tree, dest.get('marker'))
            else:
                raise Problem('Supported destinations are local folders and http observation')
            if not observed:
                raise Problem('The destination did not serve this version. Publishing is not complete.')
            receipt = dict(id=artifact[:32], destinationId=destination_id, tree=tree, artifact=artifact,
                           observed=True, observedAt=now(), createdAt=now(),
                           evidence=observed)
            records = self.history(pid)
            records.append(receipt)
            atomic_json(self._state_path(pid), records)
            return receipt

    def _observe_local(self, folder, tree):
        marker = folder / 'UNFORGE_RELEASE'
        if not marker.is_file():
            return None
        text = marker.read_text(encoding='utf-8').strip()
        if text != tree:
            return None
        return {'kind': 'local-marker', 'path': str(marker), 'tree': tree}

    def _observe_http(self, url, tree, marker):
        request = Request(url, headers={'User-Agent': 'Unforge-Release'})
        with urlopen(request, timeout=15) as response:
            body = response.read(64 * 1024).decode(errors='replace')
            header = response.headers.get('X-Unforge-Release', '')
        token = marker or tree
        if token not in body and header != tree:
            return None
        return {'kind': 'http', 'url': url, 'tree': tree}

    def rollback(self, pid, destination_id):
        records = [item for item in self.history(pid) if item.get('destinationId') == destination_id and item.get('observed')]
        if len(records) < 2:
            raise Problem('No previous observed release to restore')
        previous = records[-2]
        return self.publish(pid, destination_id, tree=previous['tree'])
