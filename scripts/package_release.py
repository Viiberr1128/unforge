#!/usr/bin/env python3
"""Package source and built UI; never include user projects or local secrets."""
import hashlib
import gzip
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ['README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'CONTRIBUTING.md', 'SECURITY.md', 'AGENTS.md',
              'package.json', 'package-lock.json', 'vite.config.js', 'index.html', '.gitignore',
              'engine.py', 'launcher.py', 'test_launcher.py', 'unforge.py', 'agent_jobs.py', 'test_agent_jobs.py', 'insights.py', 'test_insights.py', 'test_engine.py', 'test_client.py',
              'operations.py', 'care.py', 'recovery.py', 'test_operations.py', 'test_care.py', 'test_recovery.py',
              'projects.py', 'drafts.py', 'runtime.py', 'backups.py', 'backup_scheduler.py', 'workspace_watch.py', 'test_workspace_watch.py',
              'incremental_backups.py', 'test_incremental_backups.py', 'test_cloud_rehearsal.py',
              'test_projects.py', 'test_drafts.py', 'test_runtime.py', 'test_backups.py', 'test_storage_guard.py', 'test_workspace_durability.py', 'test_backup_metadata.py', 'test_backup_scheduler.py', 'test_agent_durability.py', 'test_ownership_integration.py', 'test_cloud_status.py',
              'test_release_privacy.py', 'test_build_macos.py', 'lanes.py', 'integrate.py', 'checks.py', 'releases.py',
              'app_manifest.py', 'test_lanes.py', 'github_import.py', 'exchange.py', 'second_home.py',
              'test_github_killer.py', 'start.sh', 'start.command', 'check.sh']
DIRECTORIES = ['src', 'public', 'docs', 'tests', 'scripts', 'dist', '.github', 'macos']
EXTENSIONS = {'.py', '.js', '.jsx', '.mjs', '.css', '.html', '.svg', '.png', '.md', '.txt', '.swift', '.yml', '.yaml', '.json', '.sh', '.command', '.entitlements'}
PRIVATE_NAMES = {'auth.json', 'credentials.json', 'secrets.json', 'recovery-key', 'recovery-key.txt', 'settings.json'}


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
    artifacts.mkdir(exist_ok=True)
    output = artifacts / f'unforge-{version}.tar.gz'
    manifest = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(selected)}
    manifest_path = artifacts / 'MANIFEST.sha256.json'
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    # Tar defaults expose the build account's username and UID. Both tar and
    # gzip metadata must be independent of the build machine and current time.
    def public_metadata(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        info.mtime = epoch
        info.mode = 0o755 if info.mode & 0o111 else 0o644
        info.pax_headers = {}
        return info
    with output.open('wb') as raw, gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=epoch) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as archive:
            for path in selected:
                archive.add(path, arcname=f'unforge-{version}/{path.relative_to(ROOT)}', recursive=False, filter=public_metadata)
            archive.add(manifest_path, arcname=f'unforge-{version}/MANIFEST.sha256.json', filter=public_metadata)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(f'{digest}  {output.name}\n', encoding='utf-8')
    print(f'{output}\nSHA256 {digest}')


if __name__ == '__main__':
    main()
