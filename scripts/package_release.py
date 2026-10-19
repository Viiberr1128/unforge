#!/usr/bin/env python3
"""Package source and built UI; never include user projects or local secrets."""
import hashlib
import gzip
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile

if __package__:
    from .build_macos import promote_artifacts
else:
    from build_macos import promote_artifacts

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ['README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'CONTRIBUTING.md', 'SECURITY.md', 'AGENTS.md',
              'package.json', 'package-lock.json', 'vite.config.js', 'index.html', '.gitignore',
              'engine.py', 'launcher.py', 'test_launcher.py', 'unforge.py', 'agent_jobs.py', 'agent_plans.py', 'test_agent_jobs.py', 'test_agent_plans.py', 'test_fleet_bridge.py', 'test_public_source_check.py', 'insights.py', 'test_insights.py', 'test_engine.py', 'test_client.py',
              'operations.py', 'care.py', 'recovery.py', 'test_operations.py', 'test_care.py', 'test_recovery.py',
              'projects.py', 'drafts.py', 'runtime.py', 'backups.py', 'backup_scheduler.py', 'workspace_watch.py', 'test_workspace_watch.py',
              'incremental_backups.py', 'test_incremental_backups.py', 'test_cloud_rehearsal.py',
              'test_projects.py', 'test_drafts.py', 'test_runtime.py', 'test_backups.py', 'test_storage_guard.py', 'test_workspace_durability.py', 'test_backup_metadata.py', 'test_backup_scheduler.py', 'test_agent_durability.py', 'test_ownership_integration.py', 'test_cloud_status.py',
              'test_release_privacy.py', 'test_build_macos.py', 'lanes.py', 'integrate.py', 'checks.py', 'releases.py',
              'app_manifest.py', 'test_lanes.py', 'github_import.py', 'exchange.py', 'second_home.py',
              'test_github_killer.py', 'build_info.py', 'change_feed.py', 'operating.py', 'recovery_status.py',
              'test_operating.py', 'test_recovery_status.py', 'test_core_repairs.py', 'test_check_release_repairs.py', 'test_status_reads.py',
              'recovery_links.py', 'test_lane_recovery.py', 'lane_review.py', 'test_lane_review.py',
              'start.sh', 'start.command', 'check.sh']
DIRECTORIES = ['src', 'public', 'docs', 'tests', 'scripts', 'dist', 'macos']
EXTENSIONS = {'.py', '.js', '.jsx', '.mjs', '.css', '.html', '.svg', '.png', '.md', '.txt', '.swift', '.yml', '.yaml', '.json', '.sh', '.command', '.entitlements'}
PRIVATE_NAMES = {'auth.json', 'credentials.json', 'secrets.json', 'recovery-key', 'recovery-key.txt', 'settings.json'}
MAX_SOURCE_BYTES = 512 * 1024 * 1024


def git_identity():
    """Only this checkout can identify these source bytes, never an ancestor."""
    env = {key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
    def git(*args):
        return subprocess.run(['git', '-C', str(ROOT), *args], env=env, capture_output=True, timeout=10)
    top = git('rev-parse', '--show-toplevel')
    if top.returncode or Path(top.stdout.decode().strip()).resolve() != ROOT.resolve():
        return {'revision':None, 'status':None}
    revision = git('rev-parse', 'HEAD')
    status = git('status', '--porcelain', '--', '.', ':(exclude)artifacts')
    if status.returncode:
        raise SystemExit('Could not capture the source checkout identity.')
    return {'revision':revision.stdout.decode().strip() if revision.returncode == 0 else None,
            'status':status.stdout}


def read_source(path, destination=None):
    """Hash the same regular file bytes that are copied to private staging."""
    path = Path(path)
    if not path.is_relative_to(ROOT): raise SystemExit('Source file leaves the release root.')
    for part in [path, *path.parents]:
        if part.is_symlink(): raise SystemExit('Refusing to package linked source.')
        if part == ROOT: break
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SOURCE_BYTES:
            raise SystemExit('Source file is not a supported regular file.')
        digest, size = hashlib.sha256(), 0
        output = None
        try:
            if destination is not None:
                destination.parent.mkdir(parents=True, exist_ok=True)
                output = destination.open('xb')
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_SOURCE_BYTES: raise SystemExit('Source snapshot exceeds its supported size.')
                digest.update(chunk)
                if output is not None: output.write(chunk)
            after = os.fstat(source.fileno())
            identity = lambda info:(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)
            if identity(before) != identity(after) or size != before.st_size:
                raise SystemExit('Source changed during capture; previous artifacts are preserved.')
        finally:
            if output is not None: output.close()
    mode = 0o755 if before.st_mode & 0o111 else 0o644
    if destination is not None: destination.chmod(mode)
    return digest.hexdigest(), size, mode


def verify_capture(selected, manifest, modes, identity):
    if release_files() != selected or git_identity() != identity:
        raise SystemExit('Source files or Git identity changed during packaging; previous artifacts are preserved.')
    for path in selected:
        name = path.relative_to(ROOT).as_posix()
        checksum, _, mode = read_source(path)
        if checksum != manifest[name] or mode != modes[name]:
            raise SystemExit('Source changed during packaging; previous artifacts are preserved.')
    if git_identity() != identity:
        raise SystemExit('Git identity changed during packaging; previous artifacts are preserved.')


def release_files():
    """An explicit source envelope; Git checkouts must not publish stray files."""
    tracked = None
    if (ROOT / '.git').exists():
        result = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], capture_output=True, check=True)
        tracked = set(result.stdout.decode('utf-8').split('\0'))
    selected = [ROOT / name for name in ROOT_FILES]
    for directory in DIRECTORIES:
        base = ROOT / directory
        if base.is_symlink():
            raise SystemExit('Refusing to package a linked source directory: ' + directory)
        for current, dirs, files in os.walk(base, followlinks=False):
            for name in dirs:
                if (Path(current) / name).is_symlink():
                    raise SystemExit('Refusing to package a linked source directory.')
            dirs[:] = [name for name in dirs if name != '__pycache__']
            selected.extend(Path(current) / name for name in files if name != '.DS_Store' and not name.endswith('.pyc'))
    for path in selected:
        relative = path.relative_to(ROOT)
        if path.is_symlink() or not path.is_file():
            raise SystemExit('Refusing to package a non-regular source file: ' + str(relative))
        if (any(part.startswith('.') and part not in ('.github', '.gitignore') for part in relative.parts)
                or path.name.lower() in PRIVATE_NAMES or path.suffix.lower() in ('.sqlite', '.sqlite3', '.db', '.map', '.pem', '.key')):
            raise SystemExit('Refusing to package private state or source maps: ' + str(relative))
        if relative.parts[0] == 'dist':
            if path.suffix.lower() not in {'.html', '.js', '.css', '.svg', '.png', '.woff', '.woff2'}:
                raise SystemExit('Unexpected built interface file: ' + str(relative))
        elif str(relative) not in ROOT_FILES and path.suffix.lower() not in EXTENSIONS:
            raise SystemExit('Unexpected source file type: ' + str(relative))
        if tracked is not None and relative.parts[0] != 'dist' and str(relative) not in tracked:
            raise SystemExit('Refusing to package an untracked source file: ' + str(relative))
    return sorted(selected)


def main():
    version = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))['version']
    if not isinstance(version, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?', version):
        raise SystemExit('Invalid release version.')
    epoch = int(os.environ.get('SOURCE_DATE_EPOCH', '0'))
    if not 0 <= epoch <= 4294967295:
        raise SystemExit('SOURCE_DATE_EPOCH must fit an unsigned 32-bit timestamp.')
    if not (ROOT / 'dist/index.html').is_file():
        raise SystemExit('Run npm ci and npm run build before packaging.')
    missing = [name for name in ROOT_FILES if not (ROOT / name).is_file()]
    missing.extend(name for name in DIRECTORIES if not (ROOT / name).is_dir())
    if missing:
        raise SystemExit('Required release files are missing: ' + ', '.join(missing))
    selected = release_files()
    artifacts = ROOT / 'artifacts'
    if artifacts.is_symlink(): raise SystemExit('Refusing to package into linked release storage.')
    artifacts.mkdir(exist_ok=True)
    # Tar defaults expose the build account's username and UID. Both tar and
    # gzip metadata must be independent of the build machine and current time.
    def public_metadata(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        info.mtime = epoch
        info.mode = 0o755 if info.mode & 0o111 else 0o644
        info.pax_headers = {}
        return info
    identity = git_identity()
    with tempfile.TemporaryDirectory(prefix='.source-release-', dir=artifacts) as temporary:
        staging = Path(temporary); snapshot = staging / 'snapshot'
        manifest, modes, total = {}, {}, 0
        for path in selected:
            name = path.relative_to(ROOT).as_posix()
            checksum, size, mode = read_source(path, snapshot / name)
            total += size
            if total > MAX_SOURCE_BYTES: raise SystemExit('Source snapshot exceeds its supported size.')
            manifest[name], modes[name] = checksum, mode
        if json.loads((snapshot / 'package.json').read_text())['version'] != version:
            raise SystemExit('Source version changed while packaging; previous artifacts are preserved.')
        captured_manifest = dict(manifest)
        receipt = {'schemaVersion':1, 'format':'unforge-source-release', 'version':version,
                   'sourceRevision':identity['revision'],
                   'sourceDirty':bool(identity['status']) if identity['status'] is not None else None,
                   'sourceDigest':hashlib.sha256(json.dumps(captured_manifest, sort_keys=True).encode()).hexdigest()}
        build_path = snapshot / 'SOURCE-BUILD.json'
        build_path.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
        manifest['SOURCE-BUILD.json'] = hashlib.sha256(build_path.read_bytes()).hexdigest()
        manifest_path = staging / 'MANIFEST.sha256.json'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        output = staging / f'unforge-{version}.tar.gz'
        with output.open('xb') as raw, gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                for name in sorted(manifest):
                    archive.add(snapshot / name, arcname=f'unforge-{version}/{name}', recursive=False, filter=public_metadata)
                archive.add(manifest_path, arcname=f'unforge-{version}/MANIFEST.sha256.json', filter=public_metadata)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        checksum_path = output.with_suffix(output.suffix + '.sha256')
        checksum_path.write_text(f'{digest}  {output.name}\n', encoding='utf-8')
        verify_capture(selected, captured_manifest, modes, identity)
        final_output = artifacts / output.name
        promote_artifacts([(output, final_output), (manifest_path, artifacts / manifest_path.name),
                           (checksum_path, artifacts / checksum_path.name)])
    print(f'{final_output}\nSHA256 {digest}')


if __name__ == '__main__':
    main()
