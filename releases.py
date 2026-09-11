"""Bound destinations and observed live identity. Merge is not done until the site matches."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.request import Request, urlopen

from engine import Problem
from lanes import atomic_json

SECRET_KEY = re.compile(r'(token|secret|password|apikey|api_key|service.?role|access.?token)$', re.I)
SECRET_VALUE = re.compile(r'^(sk-|cf_|sbp_|eyJ|supabase_service)')
PAGES_PROJECT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,57}$')
SUPABASE_REF = re.compile(r'^[a-z0-9]{20}$')
ACCOUNT_ID = re.compile(r'^[a-f0-9]{32}$')


def now():
    return datetime.now(timezone.utc).isoformat()


class Releases:
    def __init__(self, engine, checks=None, run_command=None):
        self.engine = engine
        self.checks = checks
        self.run_command = run_command
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
        items = self.document(pid).get('destinations') or []
        if not isinstance(items, list):
            raise Problem('Destinations must be a list')
        return items

    def validate_destination(self, dest):
        if not isinstance(dest, dict):
            raise Problem('Destination must be an object')
        for key, value in dest.items():
            if SECRET_KEY.search(str(key).replace('-', '_')):
                raise Problem('Do not store API tokens in the project. Sign in with Wrangler or the Supabase CLI on this computer, using your own account.')
            if isinstance(value, str) and SECRET_VALUE.search(value.strip()):
                raise Problem('That looks like a secret. Keep it in your CLI login, not in app settings.')
        dest_id = dest.get('id')
        if not isinstance(dest_id, str) or not dest_id.strip() or '/' in dest_id:
            raise Problem('Each destination needs a short name')
        kind = dest.get('type')
        if kind == 'local':
            if not isinstance(dest.get('path'), str) or not dest.get('path'):
                raise Problem('Local destination needs a folder path')
        elif kind == 'http':
            self._require_public_url(dest.get('url'), allow_loopback=True)
        elif kind == 'cloudflare-pages':
            if not isinstance(dest.get('project'), str) or not PAGES_PROJECT.match(dest['project']):
                raise Problem('Cloudflare Pages project names use letters, numbers, and dashes. Create the project in your Cloudflare account.')
            self._require_public_url(dest.get('url'), allow_loopback=True)
            directory = dest.get('directory') or 'dist'
            if not isinstance(directory, str) or '..' in Path(directory).parts or Path(directory).is_absolute():
                raise Problem('Static folder must be a path inside the project, such as dist or site')
            dest = dict(dest)
            dest['directory'] = directory
            profile = dest.get('wranglerProfile')
            if profile is not None and (not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', profile)):
                raise Problem('Wrangler profile names are short identifiers. Leave this blank to use your default login.')
            account = dest.get('accountId')
            if account is not None and (not isinstance(account, str) or not ACCOUNT_ID.match(account)):
                raise Problem('If you set an account id, it must be the 32-character id from YOUR Cloudflare dashboard.')
        elif kind == 'supabase-functions':
            if not isinstance(dest.get('projectRef'), str) or not SUPABASE_REF.match(dest['projectRef']):
                raise Problem('Use your own Supabase project ref. Unforge does not provide a shared database.')
            self._require_public_url(dest.get('url'), allow_loopback=True)
        else:
            raise Problem('Supported destinations are a local folder, https observation, Cloudflare Pages, or Supabase functions')
        return dest

    def _require_public_url(self, url, allow_loopback=False):
        if not isinstance(url, str):
            raise Problem('Bind the https URL you own. Any domain works — .com, .ai, or otherwise.')
        if url.startswith('https://'):
            return
        if allow_loopback and url.startswith('http://127.0.0.1'):
            return
        raise Problem('Live URLs must be https. Use the hostname you already own.')

    def bind(self, pid, destination):
        destination = self.validate_destination(destination)
        data = dict(self.document(pid))
        items = [item for item in (data.get('destinations') or []) if isinstance(item, dict)]
        replaced = False
        for index, item in enumerate(items):
            if item.get('id') == destination['id']:
                items[index] = destination
                replaced = True
                break
        if not replaced:
            items.append(destination)
        data['destinations'] = items
        data.pop('githubAbsent', None)
        data['schemaVersion'] = 1
        text = json.dumps(data, indent=2) + '\n'
        path = self.engine.root(pid) / '.unforge' / 'app.json'
        expected = path.read_text(encoding='utf-8') if path.is_file() else None
        self.engine.edit(pid, '.unforge/app.json', text, expected)
        return destination

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
            dest = self.validate_destination(dest)
            artifact = hashlib.sha256(tree.encode()).hexdigest()
            observed = self._publish_kind(pid, root, dest, tree)
            if not observed:
                raise Problem('The destination did not serve this version. Publishing is not complete.')
            receipt = dict(id=artifact[:32], destinationId=destination_id, tree=tree, artifact=artifact,
                           observed=True, observedAt=now(), createdAt=now(),
                           evidence=observed)
            records = self.history(pid)
            records.append(receipt)
            atomic_json(self._state_path(pid), records)
            return receipt

    def _publish_kind(self, pid, root, dest, tree):
        kind = dest['type']
        if kind == 'local':
            target = dest.get('path')
            folder = Path(target)
            if not folder.is_absolute():
                folder = root / target
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'UNFORGE_RELEASE').write_text(tree + '\n', encoding='utf-8')
            return self._observe_local(folder, tree)
        if kind == 'http':
            return self._observe_http(dest['url'], tree, dest.get('marker'))
        if kind == 'cloudflare-pages':
            staging = self._stage_static(pid, root, dest.get('directory') or 'dist', tree)
            self._wrangler_pages_deploy(root, dest, staging, tree)
            return self._observe_http(dest['url'], tree, dest.get('marker') or tree)
        if kind == 'supabase-functions':
            self._supabase_functions_deploy(root, dest)
            return self._observe_http(dest['url'], tree, dest.get('marker') or tree)
        raise Problem('Unknown destination type')

    def _stage_static(self, pid, root, directory, tree):
        source = (root / directory).resolve()
        if not str(source).startswith(str(root.resolve())):
            raise Problem('Static folder must stay inside the project')
        if not source.is_dir():
            raise Problem('Build the site first. Unforge publishes a folder such as dist, site, or public.')
        staging = self.root / pid / 'stage'
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(source, staging, ignore=shutil.ignore_patterns('.git', 'node_modules'))
        (staging / 'UNFORGE_RELEASE').write_text(tree + '\n', encoding='utf-8')
        return staging

    def _wrangler_bin(self, root):
        if self.run_command:
            return ['wrangler']
        local = root / 'node_modules' / 'wrangler' / 'bin' / 'wrangler.js'
        node = shutil.which('node')
        if local.is_file() and node:
            return [node, str(local)]
        wrangler = shutil.which('wrangler')
        if wrangler:
            return [wrangler]
        raise Problem('Wrangler is not installed. Install it and run wrangler login with YOUR Cloudflare account. Unforge does not provide hosting.')

    def _wrangler_pages_deploy(self, root, dest, staging, tree):
        args = self._wrangler_bin(root) + ['pages', 'deploy', str(staging), '--project-name', dest['project'], '--commit-hash', tree[:40]]
        profile = dest.get('wranglerProfile')
        if profile:
            args.extend(['--profile', profile])
        extra = {}
        if dest.get('accountId'):
            extra['CLOUDFLARE_ACCOUNT_ID'] = dest['accountId']
        self._run(args, extra)

    def _supabase_functions_deploy(self, root, dest):
        functions = root / 'supabase' / 'functions'
        if not functions.is_dir():
            raise Problem('This project has no supabase/functions folder. Only connect Supabase when the app actually uses it.')
        binary = 'supabase' if self.run_command else shutil.which('supabase')
        if not binary:
            raise Problem('The Supabase CLI is not installed. Install it and sign in to YOUR project. Unforge does not provide a shared database.')
        args = [binary, 'functions', 'deploy', '--project-ref', dest['projectRef'], '--use-api']
        import_map = root / 'deno.json'
        if import_map.is_file():
            args.extend(['--import-map', str(import_map)])
        self._run(args)

    def _run(self, args, extra_env=None):
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        if self.run_command:
            result = self.run_command(args, env)
            if getattr(result, 'returncode', 0):
                raise Problem('Publish command failed. Sign in to your own provider account and retry.')
            return result
        result = subprocess.run(args, env=env, capture_output=True, timeout=180)
        if result.returncode != 0:
            raise Problem('Publish command failed. Sign in to your own provider account and retry.')
        return result

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
