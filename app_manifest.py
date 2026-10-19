"""Computed GitHub-absence status. This flag cannot be set by hand."""
import json
from pathlib import Path

from engine import Problem
from checks import resolve_commit


class AppManifest:
    def __init__(self, engine, checks=None, releases=None, lanes=None):
        self.engine = engine
        self.checks = checks
        self.releases = releases
        self.lanes = lanes

    def get(self, pid):
        root = self.engine.root(pid)
        blockers = []
        remotes = self.engine.git(root, 'remote', '-v', allowed_returncodes=(0, 128)).decode(errors='replace')
        if 'github.com' in remotes.lower():
            blockers.append('An optional GitHub remote is configured.')
        workflows = list((root / '.github' / 'workflows').glob('*.yml')) + list((root / '.github' / 'workflows').glob('*.yaml'))
        if workflows:
            blockers.append('GitHub workflow files are still here. Import GitHub Actions to turn run steps into local checks.')
        tree = self.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
        document = {}
        path = root / '.unforge' / 'app.json'
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    document = loaded
            except (ValueError, OSError, UnicodeError):
                blockers.append('App settings could not be read.')
        if document.get('githubAbsent') is True:
            # A stored true is never trusted; only computed absence counts.
            blockers.append('You cannot mark GitHub gone by hand. Unforge looks at remotes, checks, and the live site.')
        checks_status = 'not-configured'
        checked = None
        if self.checks:
            try:
                graph = self.checks.graph(pid, tree=tree)
                if graph:
                    checked = self.checks.result(pid, tree, graph)
                    checks_status = checked.get('status') if checked else 'not-checked'
            except Problem:
                checks_status = 'invalid-configuration'
        if checks_status != 'passed':
            blockers.append('Configure local checks.' if checks_status == 'not-configured' else 'This version has not passed Unforge checks yet.')
        production = None
        if document.get('productionRef'):
            try:
                production = resolve_commit(self.engine, root, document['productionRef'])
            except Problem:
                blockers.append('The production source ref could not be resolved.')
        observed = self.releases.last_observed(pid) if self.releases else None
        expected = production or tree
        current_observed = self.releases.last_observed(pid, tree=expected) if self.releases else None
        if not current_observed:
            blockers.append('Observe the live site for this source version.')
        status = dict(schemaVersion=1, githubAbsent=not blockers, blockers=blockers,
                      destinations=document.get('destinations') or [],
                      checks=document.get('checks'),
                      sourceVersion=tree, productionRef=document.get('productionRef'), productionVersion=production,
                      sourceDirty=bool(self.engine.git(root, 'status', '--porcelain')),
                      checksStatus=checks_status,
                      checkedAt=checked.get('createdAt') if checked else None,
                      liveVersion=observed.get('tree') if observed else None,
                      observedAt=observed.get('observedAt') if observed else None,
                      readiness=observed.get('readiness') if observed else None)
        return status

    def save(self, pid, document, expected=None):
        if not isinstance(document, dict):
            raise Problem('App settings must be an object')
        if document.get('githubAbsent') is True:
            raise Problem('You cannot mark GitHub gone by hand. Unforge looks at remotes, checks, and the live site.')
        payload = dict(document)
        if payload.get('checks') is not None and self.checks:
            self.checks.graph(pid, payload['checks'])
        if payload.get('destinations') is not None and self.releases:
            if not isinstance(payload['destinations'], list):
                raise Problem('Destinations must be a list')
            payload['destinations'] = [self.releases.validate_destination(item) for item in payload['destinations']]
        if payload.get('productionRef'):
            resolve_commit(self.engine, self.engine.root(pid), payload['productionRef'])
        payload.pop('githubAbsent', None)
        payload['schemaVersion'] = 1
        text = json.dumps(payload, indent=2) + '\n'
        return self.engine.edit(pid, '.unforge/app.json', text, expected)
