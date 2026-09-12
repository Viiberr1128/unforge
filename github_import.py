"""Turn GitHub Actions run steps into local Unforge checks.

`uses:` steps, secrets, and hosted runners are recorded as leftovers. Unforge
does not pretend those still need github.com after the owner archives them.
"""
import json
import re
import shlex
from pathlib import Path
import shutil
import uuid

from engine import Problem
from lanes import atomic_json

SECRET_EXPR = re.compile(r'\$\{\{\s*(secrets|vars)\.')
UNSAFE = re.compile(r'(curl\s+[^\n]*\| *(ba)?sh|sudo |rm\s+-rf\s+/)')


def parse_workflow(text):
    if not isinstance(text, str) or len(text) > 200_000:
        raise Problem('Workflow file is missing or too large')
    runs, skipped, secrets = [], [], False
    block = None
    block_indent = 0
    for raw in text.splitlines():
        if block is not None:
            indent = len(raw) - len(raw.lstrip(' '))
            if raw.strip() and indent > block_indent:
                block.append(raw.strip())
                continue
            command = '\n'.join(block).strip()
            if command:
                runs.append(command)
            block = None
        stripped = raw.strip()
        if stripped.startswith('- '):
            stripped = stripped[2:].strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.startswith('uses:'):
            skipped.append(stripped.split(':', 1)[1].strip().strip("'\""))
            continue
        if SECRET_EXPR.search(stripped):
            secrets = True
            continue
        if stripped.startswith('run:'):
            rest = stripped[4:].strip()
            if rest in {'|', '>', '|-', '>-', ''} or rest.startswith('|') or rest.startswith('>'):
                block = []
                block_indent = len(raw) - len(raw.lstrip(' '))
            else:
                runs.append(rest.strip("'\""))
    if block:
        command = '\n'.join(block).strip()
        if command:
            runs.append(command)
    return {'runs': runs, 'skippedUses': skipped, 'secretsReferenced': secrets}


def command_for(run):
    if not run or len(run) > 4000 or '\0' in run:
        raise Problem('A workflow run step is empty or too large')
    if SECRET_EXPR.search(run) or UNSAFE.search(run) or re.search(
            r'GITHUB_|RUNNER_|_ACCESS_TOKEN|_API_TOKEN|_SECRET|wrangler\.js deploy|functions deploy', run):
        return None
    if '\n' in run:
        return ['bash', '-lc', run]
    try:
        parts = shlex.split(run)
    except ValueError:
        return ['bash', '-lc', run]
    return parts or None


class GitHubImport:
    def __init__(self, engine):
        self.engine = engine

    def inspect(self, pid):
        root = self.engine.root(pid)
        folder = root / '.github' / 'workflows'
        files = []
        if folder.is_dir():
            files = sorted(path for path in folder.iterdir() if path.suffix in {'.yml', '.yaml'} and path.is_file())
        remotes = self.engine.git(root, 'remote', '-v', allowed_returncodes=(0, 128)).decode(errors='replace')
        github_remotes = []
        for line in remotes.splitlines():
            if 'github.com' in line.lower():
                name = line.split()[0]
                if name not in github_remotes:
                    github_remotes.append(name)
        return {
            'workflowFiles': [str(path.relative_to(root)) for path in files],
            'githubRemotes': github_remotes,
            'needsGitHub': bool(files or github_remotes),
        }

    def import_workflows(self, pid):
        root = self.engine.root(pid)
        info = self.inspect(pid)
        jobs, leftovers = [], []
        for relative in info['workflowFiles']:
            parsed = parse_workflow((root / relative).read_text(encoding='utf-8', errors='replace'))
            for skipped in parsed['skippedUses']:
                leftovers.append({'kind': 'uses', 'step': skipped, 'file': relative,
                                  'answer': 'This ran on GitHub’s machines. On your Mac, install the tool once (Node, Python, Git) and keep the project files here. Unforge already has the checkout.'})
            if parsed['secretsReferenced']:
                leftovers.append({'kind': 'secrets', 'file': relative,
                                  'answer': 'Secrets stay in your own Cloudflare or database login. Use Unforge publish instead of a GitHub deploy step.'})
            for index, run in enumerate(parsed['runs'], start=1):
                command = command_for(run)
                if not command:
                    leftovers.append({'kind': 'skipped-run', 'file': relative,
                                      'answer': 'A run step used secrets or an unsafe pipe. Do that with your own signed-in CLI, not GitHub.'})
                    continue
                slug = Path(relative).stem.replace(' ', '-')[:24]
                jobs.append({'id': f'{slug}-{index}'[:40], 'command': command, 'timeoutSeconds': 300, 'cache': True})
        if not jobs:
            jobs = [{'id': 'ok', 'command': ['python3', '-c', 'print("ok")'], 'timeoutSeconds': 30, 'cache': True}]
        graph = {'schemaVersion': 1, 'jobs': jobs[:16]}
        path = root / '.unforge' / 'app.json'
        data = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    data = loaded
            except (ValueError, OSError, UnicodeError):
                data = {}
        data['schemaVersion'] = 1
        data['checks'] = graph
        data.pop('githubAbsent', None)
        expected = path.read_text(encoding='utf-8') if path.is_file() else None
        self.engine.edit(pid, '.unforge/app.json', json.dumps(data, indent=2) + '\n', expected)
        return {'checks': graph, 'leftovers': leftovers, 'inspection': info}

    def archive_github(self, pid):
        """Remove GitHub remotes and park workflow files so the app can leave github.com."""
        root = self.engine.root(pid)
        info = self.inspect(pid)
        for remote in info['githubRemotes']:
            self.engine.git(root, 'remote', 'remove', remote, allowed_returncodes=(0, 2, 128))
        parked = []
        source = root / '.github' / 'workflows'
        if source.is_dir():
            dest = root / '.unforge' / 'archived-github-workflows'
            dest.mkdir(parents=True, exist_ok=True)
            for path in list(source.iterdir()):
                if path.is_file() and path.suffix in {'.yml', '.yaml'}:
                    target = dest / path.name
                    if target.exists():
                        target = dest / (path.stem + '-' + uuid.uuid4().hex[:8] + path.suffix)
                    shutil.move(str(path), str(target))
                    parked.append(str(target.relative_to(root)))
            try:
                source.rmdir()
                (root / '.github').rmdir()
            except OSError:
                pass
        return {'githubRemotesRemoved': info['githubRemotes'], 'parkedWorkflows': parked,
                'inspection': self.inspect(pid)}
