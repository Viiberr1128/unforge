#!/usr/bin/env python3
"""Package source and built UI; never include user projects or local secrets."""
import hashlib
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ['README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'CONTRIBUTING.md', 'SECURITY.md', 'AGENTS.md',
              'package.json', 'package-lock.json', 'vite.config.js', 'index.html', '.gitignore',
              'engine.py', 'launcher.py', 'test_launcher.py', 'unforge.py', 'agent_jobs.py', 'test_agent_jobs.py', 'insights.py', 'test_insights.py', 'test_engine.py', 'test_client.py',
              'operations.py', 'care.py', 'recovery.py', 'test_operations.py', 'test_care.py', 'test_recovery.py',
              'projects.py', 'drafts.py', 'runtime.py', 'backups.py', 'backup_scheduler.py', 'workspace_watch.py', 'test_workspace_watch.py',
              'incremental_backups.py', 'test_incremental_backups.py', 'test_cloud_rehearsal.py',
              'test_projects.py', 'test_drafts.py', 'test_runtime.py', 'test_backups.py', 'test_storage_guard.py', 'test_workspace_durability.py', 'test_backup_metadata.py', 'test_backup_scheduler.py', 'test_agent_durability.py', 'test_ownership_integration.py', 'test_cloud_status.py',
              'start.sh', 'start.command', 'check.sh']
DIRECTORIES = ['src', 'public', 'docs', 'tests', 'scripts', 'dist', '.github', 'macos']


def main():
    version = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))['version']
    if not (ROOT / 'dist/index.html').is_file():
        raise SystemExit('Run npm ci and npm run build before packaging.')
    missing = [name for name in ROOT_FILES if not (ROOT / name).is_file()]
    missing.extend(name for name in DIRECTORIES if not (ROOT / name).is_dir())
    if missing:
        raise SystemExit('Required release files are missing: ' + ', '.join(missing))
    selected = [ROOT / name for name in ROOT_FILES]
    for directory in DIRECTORIES:
        selected.extend(path for path in (ROOT / directory).rglob('*')
                        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc')
    for path in selected:
        if path.is_symlink():
            raise SystemExit(f'Refusing to package symlink: {path}')
    artifacts = ROOT / 'artifacts'
    artifacts.mkdir(exist_ok=True)
    output = artifacts / f'unforge-{version}.tar.gz'
    manifest = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(selected)}
    manifest_path = artifacts / 'MANIFEST.sha256.json'
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    with tarfile.open(output, 'w:gz') as archive:
        for path in sorted(selected):
            archive.add(path, arcname=f'unforge-{version}/{path.relative_to(ROOT)}', recursive=False)
        archive.add(manifest_path, arcname=f'unforge-{version}/MANIFEST.sha256.json')
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(f'{digest}  {output.name}\n', encoding='utf-8')
    print(f'{output}\nSHA256 {digest}')


if __name__ == '__main__':
    main()
