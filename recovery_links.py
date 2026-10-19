"""Rebind copied managed Git links without ever following the captured locations."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from engine import Problem

IDENTITY = re.compile(r'[a-f0-9]{32}\Z')
MAX_METADATA = 16 * 1024 * 1024
MAX_RECORDS = 10000


def _inside(path, root):
    path, root = Path(path), Path(root)
    if not path.is_relative_to(root): raise Problem('Recovery link leaves its workspace.')
    for part in [root, *reversed(path.relative_to(root).parents)]:
        candidate = part if part == root else root / part
        if candidate.is_symlink(): raise Problem('Recovery links cannot follow symbolic links.')
    if path.is_symlink(): raise Problem('Recovery links cannot follow symbolic links.')
    return path


def _read(path, root, limit=MAX_METADATA):
    _inside(path, root)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise Problem('Recovery link metadata is not an ordinary bounded file.')
        data = source.read(limit + 1)
    if len(data) > limit: raise Problem('Recovery link metadata exceeds its limit.')
    return data


def _text(path, root):
    try: return _read(path, root, 65536).decode('utf-8').strip()
    except UnicodeError as error: raise Problem('Invalid Git worktree link metadata.') from error


def _lexical(path, parent):
    value = Path(path)
    return Path(os.path.normpath(value if value.is_absolute() else parent / value))


def _git_admin(work, main_git, workspace, *, captured=False):
    pointer = _text(work / '.git', workspace)
    if not pointer.startswith('gitdir: ') or '\n' in pointer:
        raise Problem('Invalid managed lane Git link. Existing work has been preserved.')
    linked = _lexical(pointer.removeprefix('gitdir: '), work)
    name = linked.name
    if not name or name in ('.', '..') or len(name) > 255:
        raise Problem('Invalid managed lane Git directory.')
    admin = main_git / 'worktrees' / name
    if captured:
        # Match only the captured suffix. Never stat or open its old prefix.
        if linked.parts[-4:-1] != (main_git.parent.name, '.git', 'worktrees'):
            raise Problem('Captured lane is not linked to its captured project.')
    elif linked != admin:
        raise Problem('Lane Git link leaves its registered project. Restore its recovery point before editing.')
    _inside(admin, workspace)
    if not admin.is_dir(): raise Problem('Managed lane Git metadata is missing.')
    common = _lexical(_text(admin / 'commondir', workspace), admin)
    if common != main_git:
        raise Problem('Lane common Git directory leaves its registered project.')
    reverse = _lexical(_text(admin / 'gitdir', workspace), admin)
    if captured:
        # There are four stable components after the old workspace prefix.
        if reverse.parts[-4:] != ('.lanes', main_git.parent.name, work.name, '.git'):
            raise Problem('Captured lane reverse Git link does not match its identity.')
    elif reverse != work / '.git':
        raise Problem('Lane reverse Git link leaves its private workspace.')
    return admin


def validate_lane_repository(home, pid, lid):
    """Check links before Git can load another worktree's refs or index."""
    home = Path(home)
    if not isinstance(pid, str) or not IDENTITY.fullmatch(pid) or not isinstance(lid, str) or not IDENTITY.fullmatch(lid):
        raise Problem('Invalid managed lane identity.')
    work, main_git = home / '.lanes' / pid / lid, home / pid / '.git'
    _inside(work, home); _inside(main_git, home)
    if not work.is_dir() or not main_git.is_dir(): raise Problem('Managed lane workspace is missing.')
    if (main_git / 'commondir').exists():
        raise Problem('Project common Git directory is not private to this workspace.')
    return _git_admin(work, main_git, home)


def _atomic(path, data, root):
    _inside(path, root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.relocation-', dir=path.parent)
    try:
        if path.exists(): os.fchmod(fd, path.stat().st_mode & 0o777)
        with os.fdopen(fd, 'wb') as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
    finally: Path(temporary).unlink(missing_ok=True)


def normalize_recovered_workspace(workspace, destination):
    """Normalize only a fully verified private staging tree, before promotion.

    Link files name the final destination. No Git command is run through a
    copied worktree, and uncaptured external worktrees remain inert metadata.
    """
    from backups import MAX_BYTES, MAX_FILES, storage_guard
    workspace, destination = Path(workspace), Path(destination)
    if not workspace.is_absolute() or not destination.is_absolute(): raise Problem('Recovery paths must be absolute.')
    _inside(workspace, workspace)
    marker = workspace / 'UNFORGE-RECOVERY.json'
    original = _read(marker, workspace, 64 * 1024 * 1024)
    try: manifest = json.loads(original)
    except (ValueError, UnicodeError) as error: raise Problem('Invalid verified recovery inventory.') from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), list): raise Problem('Invalid verified recovery inventory.')
    entries = {entry['path']: dict(entry) for entry in manifest['files']}
    changed, relocated, quarantine, configurations, detached = {}, [], [], [], []
    index_root = workspace / '.lanes'
    if index_root.exists(): _inside(index_root, workspace)
    projects = [folder for folder in workspace.iterdir() if IDENTITY.fullmatch(folder.name) and folder.is_dir()]
    if len(projects) > MAX_RECORDS: raise Problem('Recovery contains too many project records.')
    for project in projects:
        pid = project.name; main_git = project / '.git'
        _inside(main_git, workspace)
        if not main_git.is_dir(): raise Problem('Recovered project repository is missing.')
        if (main_git / 'commondir').exists(): raise Problem('Recovered project has an uncaptured common Git directory.')
        alternates = main_git / 'objects/info/alternates'
        if alternates.exists() and _text(alternates, workspace):
            raise Problem('Recovered project depends on external Git objects. Existing recovery data was preserved.')
        index = index_root / (pid + '.json')
        records = []
        if index.exists():
            try: records = json.loads(_read(index, workspace))
            except (ValueError, UnicodeError) as error: raise Problem('Recovered lane index is damaged.') from error
            if not isinstance(records, list) or len(records) > MAX_RECORDS: raise Problem('Recovered lane index exceeds its limit.')
        used_admins, seen = set(), set()
        for record in records:
            lid = record.get('id') if isinstance(record, dict) else None
            if not isinstance(lid, str) or not IDENTITY.fullmatch(lid) or lid in seen:
                raise Problem('Recovered lane identity is invalid or duplicated.')
            seen.add(lid)
            work = index_root / pid / lid; final_work = destination / '.lanes' / pid / lid
            record['path'] = str(final_work)
            _inside(work, workspace)
            if work.exists():
                if not work.is_dir(): raise Problem('Recovered lane workspace is not a directory.')
                admin = _git_admin(work, main_git, workspace, captured=True)
                if admin.name in used_admins: raise Problem('Two recovered lanes share one Git index.')
                used_admins.add(admin.name)
                changed[work / '.git'] = ('gitdir: ' + str(destination / pid / '.git/worktrees' / admin.name) + '\n').encode()
                changed[admin / 'gitdir'] = (str(final_work / '.git') + '\n').encode()
                relocated.append({'projectId':pid, 'laneId':lid})
                config = admin / 'config.worktree'
                if config.exists(): configurations.append((config, final_work))
            elif record.get('status') in ('open', 'needs_restack', 'conflict'):
                raise Problem('An active recovered lane workspace is missing. Existing recovery data was preserved.')
        if records: changed[index] = (json.dumps(records, ensure_ascii=False, indent=2) + '\n').encode()
        configs = main_git / 'config'
        if configs.exists(): configurations.append((configs, destination / pid))
        admins = main_git / 'worktrees'
        if admins.exists():
            _inside(admins, workspace)
            children = list(admins.iterdir())
            if len(children) > MAX_RECORDS: raise Problem('Recovered Git worktree registrations exceed their limit.')
            for admin in children:
                _inside(admin, workspace)
                if not admin.is_dir(): raise Problem('Invalid recovered Git worktree registration.')
                if admin.name in used_admins: continue
                target = workspace / '.recovered-git-worktrees' / pid / admin.name
                _inside(target, workspace)
                if target.exists(): raise Problem('Recovered worktree archive collision. Existing metadata was preserved.')
                quarantine.append((admin, target))
                head = _text(admin / 'HEAD', workspace)
                if re.fullmatch('[a-f0-9]{40,64}', head):
                    ref = main_git / 'refs/unforge-recovered-worktrees' / hashlib.sha256(admin.name.encode()).hexdigest()
                    if ref.exists(): raise Problem('Recovered detached Git reference collision.')
                    changed[ref] = (head + '\n').encode(); detached.append(ref)
    # Inspect config with explicit --file: never discover the captured repository.
    configuration_edits = []
    env = {key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    for config, final_work in configurations:
        _read(config, workspace)
        includes = subprocess.run(['git','config','--no-includes','--file',str(config),
                                   '--name-only','--get-regexp',r'^include(if\..*)?\.path$'],
                                  env=env, capture_output=True, timeout=10)
        if includes.returncode not in (0,1): raise Problem('Recovered Git configuration is invalid.')
        if includes.returncode == 0:
            raise Problem('Recovered Git configuration includes uncaptured settings. Rebind those settings explicitly before recovery; no original configuration was read.')
        result = subprocess.run(['git','config','--no-includes','--file',str(config),'--get','core.worktree'], env=env, capture_output=True, timeout=10)
        if result.returncode not in (0,1): raise Problem('Recovered Git configuration is invalid.')
        if result.returncode == 0: configuration_edits.append((config, final_work))
    if not changed and not quarantine and not configuration_edits:
        return {'schemaVersion':1,'managedLanesRelocated':0,'externalWorktreesQuarantined':0,'detachedHeadsPreserved':0,
                'restoredInventoryRegenerated':False,'restoredFileCount':manifest['fileCount'],
                'scope':'No copied Git links required relocation.'}
    original_hash = hashlib.sha256(original).hexdigest()
    archive = workspace / '.recovery-links' / original_hash
    capture_path, receipt_path = archive / 'CAPTURE.json', archive / 'RELOCATION.json'
    if capture_path.exists() or receipt_path.exists(): raise Problem('Recovery relocation evidence already exists.')
    storage_guard([(workspace, 2 * len(original) + sum(len(data) for data in changed.values()) + 1024 * 1024,
                    'recovered Git linkage and inventory normalization')])
    for path, data in changed.items(): _atomic(path, data, workspace)
    for config, final_work in configuration_edits:
        result = subprocess.run(['git','config','--no-includes','--file',str(config),'--replace-all','core.worktree',str(final_work)],
                                env=env, capture_output=True, timeout=10)
        if result.returncode: raise Problem('Recovered Git configuration could not be relocated.')
        changed[config] = _read(config, workspace)
    for admin, target in quarantine:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        old, new = admin.relative_to(workspace).as_posix(), target.relative_to(workspace).as_posix()
        for name in list(entries):
            if name.startswith(old + '/'):
                value = entries.pop(name); value['path'] = new + name[len(old):]; entries[value['path']] = value
        os.rename(admin, target)
    restored_count = len(set(entries) | {path.relative_to(workspace).as_posix() for path in changed}
                         | {capture_path.relative_to(workspace).as_posix(), receipt_path.relative_to(workspace).as_posix()})
    report = {'schemaVersion':1,'managedLanesRelocated':len(relocated),'externalWorktreesQuarantined':len(quarantine),
              'detachedHeadsPreserved':len(detached),'gitConfigurationsRelocated':len(configuration_edits),
              'restoredInventoryRegenerated':True,'restoredFileCount':restored_count,
              'verifiedCaptureManifestSha256':original_hash,
              'captureInventoryPath':capture_path.relative_to(workspace).as_posix(),
              'normalizationReceiptPath':receipt_path.relative_to(workspace).as_posix(),
              'destination':str(destination),
              'scope':'Captured bytes were verified before relocation. Managed lane links now name this recovered workspace; uncaptured external worktree registrations are preserved as inactive metadata. No original checkout was accessed or application started.'}
    receipt = (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode()
    _atomic(capture_path, original, workspace); _atomic(receipt_path, receipt, workspace)
    changed[capture_path], changed[receipt_path] = original, receipt
    for path, data in changed.items():
        name = path.relative_to(workspace).as_posix()
        entries[name] = {'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'sqlite':False}
    if len(entries) > MAX_FILES or sum(entry['bytes'] for entry in entries.values()) > MAX_BYTES:
        raise Problem('Normalized recovery inventory exceeds its supported limit. Nothing was promoted.')
    manifest.update(files=sorted(entries.values(),key=lambda entry:entry['path']),fileCount=len(entries),
                    sourceBytes=sum(entry['bytes'] for entry in entries.values()),recoveryNormalization=report)
    _atomic(marker, (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode(), workspace)
    return report
