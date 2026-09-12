"""Computed GitHub-absence status. This flag cannot be set by hand."""
import json
from pathlib import Path

from engine import Problem


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
            blockers.append('A GitHub remote is still configured. Import Actions if you need them, then leave GitHub for this app.')
        workflows = list((root / '.github' / 'workflows').glob('*.yml')) + list((root / '.github' / 'workflows').glob('*.yaml'))
        if workflows:
            blockers.append('GitHub workflow files are still here. Import GitHub Actions to turn run steps into local checks.')
        tree = self.engine.git(root, 'rev-parse', 'HEAD').decode().strip()
        if self.checks and not self.checks.passed(pid, tree):
            blockers.append('This version has not passed Unforge checks yet.')
        if self.releases and not self.releases.last_observed(pid):
            blockers.append('After you publish, open the live site and make sure it is this version.')
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
        status = dict(schemaVersion=1, githubAbsent=not blockers, blockers=blockers,
                      destinations=document.get('destinations') or [],
                      checks=document.get('checks'),
                      liveVersion=tree)
        return status

    def save(self, pid, document, expected=None):
        if not isinstance(document, dict):
            raise Problem('App settings must be an object')
        if document.get('githubAbsent') is True:
            raise Problem('You cannot mark GitHub gone by hand. Unforge looks at remotes, checks, and the live site.')
        payload = dict(document)
        payload.pop('githubAbsent', None)
        payload['schemaVersion'] = 1
        text = json.dumps(payload, indent=2) + '\n'
        return self.engine.edit(pid, '.unforge/app.json', text, expected)
