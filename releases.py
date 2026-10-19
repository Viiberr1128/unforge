"""Immutable release artifacts and explicit observation of existing hosting."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from engine import Problem
from lanes import atomic_json
from checks import artifact_manifest, copy_artifact, digest, materialize_commit, resolve_commit, run_bounded, prune_static_artifacts
from runtime import heavy_lock

SECRET_KEY = re.compile(r'(token|secret|password|apikey|api_key|service.?role|access.?token)$', re.I)
SECRET_VALUE = re.compile(r'^(sk-|cf_|sbp_|eyJ|supabase_service)')
IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
SUPABASE_REF = re.compile(r'^[a-z0-9]{20}$')
ACCOUNT_ID = re.compile(r'^[a-f0-9]{32}$')
OCI_LABEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$')


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
            return {'schemaVersion': 1, 'destinations': []}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError, UnicodeError) as error:
            raise Problem('App settings are damaged.') from error
        if not isinstance(data, dict):
            raise Problem('App settings must be an object')
        return data

    def destinations(self, pid):
        items = self.document(pid).get('destinations') or []
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise Problem('Destinations must be a list of objects')
        return items

    def validate_destination(self, dest):
        if not isinstance(dest, dict):
            raise Problem('Destination must be an object')
        for key, value in dest.items():
            if SECRET_KEY.search(str(key).replace('-', '_')) or isinstance(value, str) and SECRET_VALUE.search(value.strip()):
                raise Problem('Do not store API tokens in the project. Use your own signed-in provider CLI.')
        if not isinstance(dest.get('id'), str) or not IDENTIFIER.fullmatch(dest['id']):
            raise Problem('Each destination needs a short identifier')
        kind = dest.get('type')
        if kind == 'local':
            if not isinstance(dest.get('path'), str) or not dest['path']:
                raise Problem('Local destination needs a folder path')
        elif kind in ('http', 'cloudflare-pages', 'supabase-functions', 'fly', 'cloudflare-worker'):
            if kind not in ('fly', 'cloudflare-worker') or dest.get('url'):
                self._require_public_url(dest.get('url'), allow_loopback=True)
            if kind == 'cloudflare-pages' and not IDENTIFIER.fullmatch(str(dest.get('project', ''))):
                raise Problem('Cloudflare Pages needs your project name')
            if kind == 'supabase-functions' and not SUPABASE_REF.fullmatch(str(dest.get('projectRef', ''))):
                raise Problem('Use your own Supabase project ref')
            if kind == 'fly' and not IDENTIFIER.fullmatch(str(dest.get('app', ''))):
                raise Problem('Fly observation needs your existing app name')
            if kind == 'cloudflare-worker' and not IDENTIFIER.fullmatch(str(dest.get('worker', ''))):
                raise Problem('Worker observation needs your existing Worker name')
        else:
            raise Problem('Choose a local folder, HTTP observation, Pages, Supabase functions, Fly observation, or Worker observation')
        if kind in ('local', 'cloudflare-pages'):
            directory = dest.get('directory') or ('dist' if kind == 'cloudflare-pages' else '.')
            if not isinstance(directory, str) or Path(directory).is_absolute() or '..' in Path(directory).parts or '\\' in directory:
                raise Problem('Artifact directory must stay inside the project')
        profile = dest.get('wranglerProfile', dest.get('profile'))
        if profile is not None and (not isinstance(profile, str) or not IDENTIFIER.fullmatch(profile)):
            raise Problem('Wrangler profile names are short identifiers')
        if dest.get('accountId') is not None and not ACCOUNT_ID.fullmatch(str(dest['accountId'])):
            raise Problem('Use your own 32-character Cloudflare account id')
        if dest.get('sourceTreeLabel') is not None and not OCI_LABEL.fullmatch(str(dest['sourceTreeLabel'])):
            raise Problem('Source tree label names must be short OCI label keys')
        if dest.get('readinessUrl', dest.get('healthUrl')):
            self._require_public_url(dest.get('readinessUrl', dest.get('healthUrl')), allow_loopback=True)
        if dest.get('marker') and (not isinstance(dest['marker'], str) or '{tree}' not in dest['marker']):
            raise Problem('A release marker must contain {tree}; a constant cannot identify a version')
        return dict(dest)

    def _require_public_url(self, url, allow_loopback=False):
        if not isinstance(url, str):
            raise Problem('Bind the URL you own')
        parsed = urlsplit(url)
        if parsed.username or parsed.password or not parsed.hostname:
            raise Problem('Live URLs cannot contain credentials')
        if parsed.scheme == 'https' or allow_loopback and parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1'):
            return
        raise Problem('Live URLs must be HTTPS or local loopback')

    def bind(self, pid, destination):
        destination = self.validate_destination(destination)
        data = dict(self.document(pid))
        items = [item for item in self.destinations(pid) if item.get('id') != destination['id']]
        items.append(destination)
        data.update(schemaVersion=1, destinations=items)
        data.pop('githubAbsent', None)
        path = self.engine.root(pid) / '.unforge' / 'app.json'
        expected = path.read_text(encoding='utf-8') if path.is_file() else None
        self.engine.edit(pid, '.unforge/app.json', json.dumps(data, indent=2) + '\n', expected)
        return destination

    def _destination(self, pid, destination_id):
        dest = next((item for item in self.destinations(pid) if item.get('id') == destination_id), None)
        if not dest:
            raise Problem('Unknown destination')
        return self.validate_destination(dest)

    def _state_path(self, pid):
        return self.root / (pid + '.json')

    def _fingerprint(self, pid, dest):
        identity = dict(dest)
        if dest['type'] == 'local':
            target = Path(dest['path']).expanduser()
            if not target.is_absolute():
                target = self.engine.root(pid) / target
            identity['resolvedPath'] = str(target.resolve())
        return digest(identity)

    def history(self, pid):
        try:
            data = json.loads(self._state_path(pid).read_text(encoding='utf-8'))
        except (ValueError, OSError, UnicodeError):
            return []
        return data if isinstance(data, list) else []

    def last_observed(self, pid, destination_id=None, *, tree=None):
        bound = {item['id']: self._fingerprint(pid, self.validate_destination(item)) for item in self.destinations(pid)}
        seen = set()
        for item in reversed(self.history(pid)):
            identity = item.get('destinationId')
            if (identity in seen or identity not in bound
                    or item.get('destinationFingerprint') != bound[identity]
                    or (destination_id is not None and identity != destination_id)):
                continue
            # The newest receipt defines this destination. Older matching SHAs
            # cannot replace a newer rollback or unsuccessful observation.
            seen.add(identity)
            if item.get('observed') and (tree is None or item.get('tree') == tree):
                return item
        return None

    def _record(self, pid, dest, tree, evidence, *, observed=True, **extra):
        receipt = dict(id=digest({'tree': tree, 'destination': dest, 'at': now()})[:32],
                       destinationId=dest['id'], destinationFingerprint=self._fingerprint(pid, dest), tree=tree,
                       observed=observed, observedAt=now(), createdAt=now(), evidence=evidence, **extra)
        records = self.history(pid)
        records.append(receipt)
        atomic_json(self._state_path(pid), records)
        prune_static_artifacts(self.engine, pid)
        return receipt

    def _artifact(self, pid, source, tree, directory):
        # Checked builds may include ignored output: retain exactly that output, never current dist/.
        receipt = self.checks.receipt(pid, tree) if self.checks else None
        retained = (receipt or {}).get('artifacts', {}).get(directory)
        folder = Path(retained['path']) if retained else source / directory
        if retained:
            if not folder.resolve().is_relative_to((self.engine.home / '.check-artifacts' / pid).resolve()):
                raise Problem('The checked artifact path is invalid')
            if digest(artifact_manifest(folder)) != retained['digest']:
                raise Problem('The checked artifact changed. Run checks again.')
            return folder, retained['digest']
        checksum = digest(artifact_manifest(folder))
        target = self.root / pid / 'artifacts' / checksum
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.stage-', dir=target.parent) as temp:
                stage = Path(temp) / 'contents'
                copy_artifact(folder, stage)
                os.rename(stage, target)
        if digest(artifact_manifest(target)) != checksum:
            raise Problem('The retained release artifact changed')
        return target, checksum

    def publish(self, pid, destination_id, *, tree=None, _artifact=None):
        with self.engine.lock:
            root = self.engine.root(pid)
            if self.engine.git(root, 'status', '--porcelain'):
                raise Problem('Save a version before publishing')
            expected_head = resolve_commit(self.engine, root, 'HEAD')
            tree = resolve_commit(self.engine, root, tree or expected_head)
            dest = self._destination(pid, destination_id)
            if dest['type'] in ('http', 'fly', 'cloudflare-worker'):
                return self.observe(pid, destination_id, tree=tree)
            if not self.checks:
                raise Problem('Configure and run checks before publishing')
            checked = self.checks.run(pid, tree=tree)
            if checked['status'] != 'passed':
                raise Problem('Checks must pass on this version before it can go live')
            lease = heavy_lock()
            try:
                with tempfile.TemporaryDirectory(prefix='unforge-release-') as temp:
                    source = materialize_commit(self.engine, root, tree, Path(temp) / 'source')
                    if resolve_commit(self.engine, root, 'HEAD') != expected_head or self.engine.git(root, 'status', '--porcelain'):
                        raise Problem('Source changed while preparing the release. Review it and retry.')
                    artifact_path, checksum = None, None
                    if dest['type'] in ('local', 'cloudflare-pages'):
                        if _artifact:
                            artifact_path = Path(_artifact['artifactPath'])
                            checksum = _artifact['artifact']
                            allowed = (self.engine.home / '.check-artifacts' / pid, self.root / pid / 'artifacts')
                            if not any(artifact_path.resolve().is_relative_to(store.resolve()) for store in allowed):
                                raise Problem('The rollback artifact path is invalid')
                            if digest(artifact_manifest(artifact_path)) != checksum:
                                raise Problem('The rollback artifact changed')
                        else:
                            directory = dest.get('directory') or ('dist' if dest['type'] == 'cloudflare-pages' else '.')
                            artifact_path, checksum = self._artifact(pid, source, tree, directory)
                    evidence = self._publish_kind(pid, source, dest, tree, artifact_path)
                    if not evidence:
                        raise Problem('The destination did not serve this version. Publishing is not complete.')
                    return self._record(pid, dest, tree, evidence, artifact=checksum,
                                        artifactPath=str(artifact_path) if artifact_path else None, operation='publish',
                                        readiness=self._readiness(dest, tree))
            finally:
                os.close(lease)

    def _publish_kind(self, pid, root, dest, tree, artifact=None):
        kind = dest['type']
        if kind == 'local':
            folder = Path(dest['path'])
            if not folder.is_absolute():
                # Relative targets belong to the actual project, not the temporary source.
                folder = self.engine.root(pid) / folder
            folder = folder.expanduser().absolute()
            live = self.engine.root(pid).resolve()
            if folder.is_symlink() or folder.resolve().is_relative_to(live) or live.is_relative_to(folder.resolve()):
                raise Problem('Publish to a separate folder outside the source checkout')
            if any((folder / name).exists() for name in ('.git', '.unforge', '.app-data', 'node_modules')):
                raise Problem('Local destination contains source, dependencies, or app data. Choose a separate artifact folder.')
            if folder.exists() and not (folder / 'UNFORGE_RELEASE').is_file() and any(folder.iterdir()):
                raise Problem('Choose an empty destination or a folder already managed by Unforge')
            if folder.exists() and (folder / 'UNFORGE_RELEASE').is_file():
                prior = next((item for item in reversed(self.history(pid))
                              if item.get('observed') and item.get('evidence', {}).get('kind') == 'local'
                              and item.get('evidence', {}).get('path') == str(folder)), None)
                marker = (folder / 'UNFORGE_RELEASE').read_text().strip()
                current = self._observe_local(folder, marker)
                if current and (not prior or prior['evidence'].get('contentDigest') != current['contentDigest']):
                    raise Problem('The local destination changed outside Unforge. Preserve those changes before publishing.')
            folder.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.unforge-publish-', dir=folder.parent) as temp:
                stage = Path(temp) / 'site'
                copy_artifact(artifact, stage)
                (stage / 'UNFORGE_RELEASE').write_text(tree + '\n', encoding='utf-8')
                if folder.exists():
                    previous = Path(temp) / 'previous'
                    os.rename(folder, previous)
                    try:
                        os.rename(stage, folder)
                    except BaseException:
                        os.rename(previous, folder)
                        raise
                else:
                    os.rename(stage, folder)
            return self._observe_local(folder, tree)
        if kind == 'cloudflare-pages':
            with tempfile.TemporaryDirectory(prefix='unforge-pages-') as temp:
                stage = Path(temp) / 'site'
                copy_artifact(artifact, stage)
                (stage / 'UNFORGE_RELEASE').write_text(tree + '\n', encoding='utf-8')
                self._wrangler_pages_deploy(root, dest, stage, tree, tools_root=self.engine.root(pid))
                return self._observe_http(dest['url'], tree, dest.get('marker'))
        if kind == 'supabase-functions':
            self._supabase_functions_deploy(root, dest)
            return self._observe_http(dest['url'], tree, dest.get('marker'))
        raise Problem('Use Observe for an externally deployed destination')

    def _wrangler_bin(self, root):
        if self.run_command:
            return ['wrangler']
        # Dependencies remain in the user's source, not the isolated checked source.
        local = root / 'node_modules' / 'wrangler' / 'bin' / 'wrangler.js'
        node = shutil.which('node')
        if local.is_file() and node:
            return [node, str(local)]
        binary = shutil.which('wrangler')
        if binary:
            return [binary]
        raise Problem('Wrangler is not installed. Sign in with your own provider account.')

    def _wrangler_pages_deploy(self, root, dest, staging, tree, *, tools_root=None):
        args = self._wrangler_bin(tools_root or root) + ['pages', 'deploy', str(staging), '--project-name', dest['project'], '--commit-hash', tree]
        if dest.get('wranglerProfile', dest.get('profile')):
            args += ['--profile', dest.get('wranglerProfile', dest.get('profile'))]
        self._run(args, {'CLOUDFLARE_ACCOUNT_ID': dest['accountId']} if dest.get('accountId') else None, cwd=root)

    def _supabase_functions_deploy(self, root, dest):
        if not (root / 'supabase' / 'functions').is_dir():
            raise Problem('This project has no supabase/functions folder')
        binary = 'supabase' if self.run_command else shutil.which('supabase')
        if not binary:
            raise Problem('The Supabase CLI is not installed')
        args = [binary, 'functions', 'deploy', '--project-ref', dest['projectRef'], '--use-api']
        if (root / 'deno.json').is_file():
            args += ['--import-map', str(root / 'deno.json')]
        self._run(args, cwd=root)

    def _run(self, args, extra_env=None, *, cwd):
        env = dict(os.environ)
        env.update(extra_env or {})
        if self.run_command:
            # Test runners keep their two-argument API; expose cwd as an explicit environment fact.
            result = self.run_command(args, {**env, 'UNFORGE_COMMAND_CWD': str(cwd)})
            if getattr(result, 'returncode', 0):
                raise Problem('Provider command failed. Check your signed-in account and retry.')
            return result
        result = run_bounded(args, cwd=cwd, env=env, timeout=180)
        if result.get('timedOut') or result['exitCode'] != 0:
            raise Problem('Provider command failed or timed out. Check your signed-in account and retry.')
        return subprocess.CompletedProcess(args, result['exitCode'], stdout=result['output'], stderr='')

    def _observe_local(self, folder, tree):
        marker = folder / 'UNFORGE_RELEASE'
        if not marker.is_file() or marker.read_text(encoding='utf-8').strip() != tree:
            return None
        contents = [item for item in artifact_manifest(folder) if item['path'] != 'UNFORGE_RELEASE']
        if not contents:
            return None
        return {'kind': 'local', 'path': str(folder), 'tree': tree, 'contentDigest': digest(contents)}

    def _observe_http(self, url, tree, marker=None):
        request = Request(url, headers={'User-Agent': 'Unforge-Release', 'Cache-Control': 'no-cache'})
        with urlopen(request, timeout=15) as response:
            body = response.read(64 * 1024).decode(errors='replace')
            header = response.headers.get('X-Unforge-Release', '')
        token = marker.replace('{tree}', tree) if marker else tree
        if token not in body and header != tree:
            return None
        return {'kind': 'http', 'url': url, 'tree': tree}

    def _json_command(self, args, root, dest):
        result = self._run(args, {'CLOUDFLARE_ACCOUNT_ID': dest['accountId']} if dest.get('accountId') else None, cwd=root)
        try:
            return json.loads(result.stdout)
        except (ValueError, AttributeError, TypeError) as error:
            raise Problem('Provider returned no usable JSON observation') from error

    def _readiness(self, dest, tree):
        url = dest.get('readinessUrl', dest.get('healthUrl'))
        if not url:
            return None
        observed_at = now()
        try:
            try:
                response = urlopen(Request(url, headers={'User-Agent': 'Unforge-Readiness', 'Cache-Control': 'no-cache'}), timeout=15)
            except HTTPError as error:
                response = error
            with response:
                status = response.code
                raw = response.read(64 * 1024)
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            ok = 200 <= status < 300 and data.get('ok', data.get('status') == 'ready') is True
            if data.get('buildSha') and data['buildSha'] != tree:
                ok = False
            return {'ok': ok, 'error': None if ok else f'Readiness did not pass (HTTP {status})',
                    'httpStatus': status, 'observedAt': observed_at}
        except (OSError, URLError, TimeoutError):
            return {'ok': False, 'error': 'Readiness could not be reached', 'observedAt': observed_at}

    def observe(self, pid, destination_id, *, tree=None):
        with self.engine.lock:
            root = self.engine.root(pid)
            document = self.document(pid)
            tree = resolve_commit(self.engine, root, tree or document.get('productionRef') or 'HEAD')
            dest = self._destination(pid, destination_id)
            kind = dest['type']
            lease = heavy_lock()
            try:
                if kind == 'fly':
                    binary = 'flyctl' if self.run_command else shutil.which('flyctl') or shutil.which('fly')
                    if not binary:
                        candidate = Path.home() / '.fly/bin/flyctl'
                        binary = str(candidate) if candidate.is_file() else None
                    if not binary:
                        raise Problem('Fly CLI is not installed')
                    data = self._json_command([binary, 'status', '--app', dest['app'], '--json'], root, dest)
                    active = [item for item in data.get('Machines', []) if item.get('state') == 'started']
                    if data.get('ID') != dest['app'] or not active:
                        raise Problem('The bound Fly app has no running machine to observe')
                    expected_tree = self.engine.git(root, 'rev-parse', tree + '^{tree}').decode().strip()
                    machines = []
                    for machine in active:
                        image = machine.get('image_ref') or {}
                        labels = image.get('labels') or {}
                        source_tree_label = dest.get('sourceTreeLabel')
                        source_tree_mismatch = source_tree_label and labels.get(source_tree_label) != expected_tree
                        if labels.get('org.opencontainers.image.revision') != tree or source_tree_mismatch:
                            raise Problem('The Fly runtime does not match this source revision')
                        if not re.fullmatch(r'sha256:[0-9a-f]{64}', str(image.get('digest', ''))):
                            raise Problem('The Fly runtime has no immutable image digest')
                        machines.append({'id': machine.get('id'), 'imageDigest': image.get('digest'), 'revision': tree})
                    evidence = {'kind': 'fly', 'app': dest['app'], 'version': data.get('Version'), 'machines': machines,
                                'scope': 'started-machines', 'inactiveMachines': len(data.get('Machines', [])) - len(active), 'tree': tree}
                elif kind == 'cloudflare-worker':
                    args = self._wrangler_bin(root)
                    suffix = ['--name', dest['worker'], '--json']
                    if dest.get('wranglerProfile', dest.get('profile')):
                        suffix += ['--profile', dest.get('wranglerProfile', dest.get('profile'))]
                    deployments = self._json_command(args + ['deployments', 'list'] + suffix, root, dest)
                    versions = self._json_command(args + ['versions', 'list'] + suffix, root, dest)
                    latest = max(deployments, key=lambda item: item.get('created_on', ''), default=None)
                    if not latest or sum(item.get('percentage', 0) for item in latest.get('versions', [])) != 100:
                        raise Problem('No complete Worker deployment could be observed')
                    by_id = {item['id']: item for item in versions}
                    deployed = []
                    for part in latest['versions']:
                        if not part.get('percentage'):
                            continue
                        version = by_id.get(part['version_id']) or {}
                        message = (version.get('annotations') or {}).get('workers/message', '')
                        revisions = set(re.findall(r'(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])', message))
                        if revisions != {tree}:
                            raise Problem('Worker version metadata has no matching full Git revision. Record it on your next existing-provider release.')
                        deployed.append({'version': part['version_id'], 'percentage': part['percentage'], 'revision': tree})
                    evidence = {'kind': 'cloudflare-worker', 'worker': dest['worker'], 'deployment': latest['id'], 'versions': deployed, 'tree': tree}
                elif kind == 'local':
                    folder = Path(dest['path'])
                    if not folder.is_absolute():
                        folder = root / folder
                    evidence = self._observe_local(folder, tree)
                else:
                    evidence = self._observe_http(dest['url'], tree, dest.get('marker'))
                if not evidence:
                    raise Problem('The destination does not identify this source revision')
                return self._record(pid, dest, tree, evidence, operation='observe', readiness=self._readiness(dest, tree))
            except Problem as error:
                self._record(pid, dest, tree, {'error': str(error)}, observed=False, operation='observe')
                raise
            finally:
                os.close(lease)

    def rollback(self, pid, destination_id):
        dest = self._destination(pid, destination_id)
        if dest['type'] in ('http', 'fly', 'cloudflare-worker'):
            raise Problem('Rollback this external host with its existing release tooling, then use Observe')
        records = [item for item in self.history(pid) if item.get('destinationId') == destination_id
                   and item.get('destinationFingerprint') == self._fingerprint(pid, dest) and item.get('observed') and item.get('operation') == 'publish']
        if len(records) < 2:
            raise Problem('No previous observed release to restore')
        previous = records[-2]
        if dest['type'] in ('local', 'cloudflare-pages') and not previous.get('artifactPath'):
            raise Problem('This older release has no retained artifact')
        return self.publish(pid, destination_id, tree=previous['tree'], _artifact=previous)
