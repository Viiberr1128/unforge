#!/usr/bin/env python3
"""Build the local macOS app without global pip installs or user project data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import venv
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / 'artifacts' / 'macos'
BUILD_ENV = ROOT / '.venv-macos'
RESTIC_LICENSE_HASHES = {'0.19.1': '6f08a01a9fab5b24e139a09f15cc24a73087c7bc09e3bacf099fdf2d767bf897'}


def package_app(app, archive):
    """Publish app bytes and relative links, without Finder/xattr/owner metadata."""
    app = Path(app)
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
        for current, directories, files in os.walk(app, followlinks=False):
            # Framework symlinks must remain symlinks, never copies of targets.
            links = [name for name in directories if (Path(current) / name).is_symlink()]
            directories[:] = sorted(name for name in directories if name not in links)
            for name in sorted(files + links):
                path = Path(current) / name
                if name == '.DS_Store' or name.startswith('._'):
                    continue
                info = zipfile.ZipInfo(str(path.relative_to(app.parent)), (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                if path.is_symlink():
                    target = os.readlink(path)
                    if os.path.isabs(target) or not path.resolve().is_relative_to(app.resolve()):
                        raise RuntimeError('App archive contains a link outside the application.')
                    content = target.encode('utf-8')
                    mode = stat.S_IFLNK | 0o777
                else:
                    content = path.read_bytes()
                    mode = stat.S_IFREG | (0o755 if path.stat().st_mode & 0o111 else 0o644)
                info.external_attr = mode << 16
                output.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED)


MACHO_MAGICS = {
    b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe',
    b'\xfe\xed\xfa\xcf', b'\xfe\xed\xfa\xce',
    b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
}
NOTARY_META = Path.home() / '.config/unforge/apple/notary-key-meta.json'
ENTITLEMENTS = ROOT / 'macos/Unforge.entitlements'


def run(*args, **kwargs):
    print('+ ' + ' '.join(str(arg) for arg in args), flush=True)
    return subprocess.run([str(arg) for arg in args], cwd=ROOT, check=True, **kwargs)


def is_macho(path):
    try:
        with path.open('rb') as handle:
            magic = handle.read(4)
    except OSError:
        return False
    return magic in MACHO_MAGICS


def parse_developer_id_identities(text):
    found = []
    for line in text.splitlines():
        if 'Developer ID Application:' not in line:
            continue
        start = line.find('"')
        end = line.rfind('"')
        if 0 <= start < end:
            found.append(line[start + 1:end])
    return found


def developer_id_identity():
    configured = os.environ.get('UNFORGE_CODESIGN_IDENTITY', '').strip()
    if configured:
        return configured
    result = subprocess.run(['security', 'find-identity', '-v', '-p', 'codesigning'],
                            capture_output=True, text=True)
    found = parse_developer_id_identities(result.stdout)
    return found[0] if found else None


def notary_credentials():
    if not NOTARY_META.is_file():
        return None
    try:
        data = json.loads(NOTARY_META.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    key_id = data.get('key_id')
    key_path = data.get('key_path')
    if not key_id or not key_path:
        return None
    path = Path(str(key_path)).expanduser()
    if not path.is_file():
        return None
    return {
        'key_id': str(key_id),
        'issuer_id': str(data['issuer_id']) if data.get('issuer_id') else None,
        'key_path': path,
    }


def sign_app(app, identity, entitlements):
    nested = []
    main_binary = app / 'Contents/MacOS/Unforge'
    for path in app.rglob('*'):
        if path.is_symlink() or not path.is_file():
            continue
        if path == main_binary:
            continue
        if is_macho(path):
            nested.append(path)
    nested.sort(key=lambda path: len(path.parts), reverse=True)
    for path in nested:
        run('codesign', '--force', '--options', 'runtime', '--timestamp',
            '--sign', identity, path)
    run('codesign', '--force', '--options', 'runtime', '--timestamp',
        '--entitlements', entitlements, '--sign', identity, app)
    run('codesign', '--verify', '--deep', '--strict', '--verbose=2', app)


def notarize_app(app, archive, credentials):
    command = [
        'xcrun', 'notarytool', 'submit', archive,
        '--key', credentials['key_path'],
        '--key-id', credentials['key_id'],
        '--wait', '--timeout', '30m', '--output-format', 'json',
    ]
    if credentials['issuer_id']:
        command.extend(['--issuer', credentials['issuer_id']])
    result = run(*command, capture_output=True, text=True)
    try:
        evidence = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError('Apple did not return a valid notarization receipt.') from error
    if evidence.get('status') != 'Accepted':
        raise RuntimeError('Apple did not accept the notarization submission.')
    return {'status': 'Accepted', 'submissionId': evidence.get('id')}


def source_identity(root=ROOT):
    """Capture inputs before compilation, then require the same inputs afterward."""
    root = Path(root)
    files = [p for p in root.glob('*.py') if p.is_file()]
    files.extend(root / name for name in ('package.json', 'package-lock.json', 'vite.config.js', 'index.html'))
    for folder in ('src', 'macos', 'scripts', 'dist'):
        files.extend(p for p in (root / folder).rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
    manifest = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(files)}
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    top = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=root, env=env,
                         capture_output=True, text=True, timeout=10)
    revision, dirty = None, None
    if top.returncode == 0 and Path(top.stdout.strip()).resolve() == root.resolve():
        commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, env=env,
                                capture_output=True, text=True, timeout=10)
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=root, env=env,
                                capture_output=True, text=True, timeout=10)
        revision = commit.stdout.strip() if commit.returncode == 0 else None
        dirty = bool(status.stdout) if status.returncode == 0 else None
    return {'sourceRevision': revision,
            'sourceDirty': dirty,
            'sourceDigest': hashlib.sha256(manifest_bytes).hexdigest(),
            'manifestBytes': manifest_bytes}


def require_source_identity(expected, root=ROOT):
    if source_identity(root) != expected:
        raise RuntimeError('Source files or Git identity changed during compilation. Start again; the prior release is preserved.')


def promote_artifacts(items):
    """Replace a completed release together; restore every prior output on failure."""
    items = [(Path(source), Path(destination)) for source, destination in items]
    if not items or len({destination for _, destination in items}) != len(items):
        raise RuntimeError('Invalid release promotion paths.')
    parent = items[0][1].parent
    for source, destination in items:
        if destination.parent != parent or source == destination or source.is_symlink() or not source.exists():
            raise RuntimeError('Invalid release promotion paths.')
        if destination.is_symlink():
            raise RuntimeError('Refusing to replace a linked release output.')
    backup = Path(tempfile.mkdtemp(prefix='.previous-release-', dir=parent))
    saved, installed = [], []
    try:
        for index, (_, destination) in enumerate(items):
            if destination.exists():
                previous = backup / str(index)
                destination.replace(previous)
                saved.append((previous, destination))
        for source, destination in items:
            source.replace(destination)
            installed.append(destination)
    except BaseException as error:
        recovery_errors = []
        for destination in reversed(installed):
            try:
                if destination.is_dir() and not destination.is_symlink():
                    shutil.rmtree(destination)
                else:
                    destination.unlink(missing_ok=True)
            except OSError as recovery_error:
                recovery_errors.append(recovery_error)
        for previous, destination in reversed(saved):
            try:
                previous.replace(destination)
            except OSError as recovery_error:
                recovery_errors.append(recovery_error)
        if recovery_errors:
            raise RuntimeError(f'Release promotion failed; preserved previous artifacts in {backup}.') from error
        shutil.rmtree(backup)
        raise
    shutil.rmtree(backup)


def finish_release(app, staging, artifacts, version, architecture, credentials, receipt):
    """Verify staged bytes before making any app, archive, or receipt public."""
    staging, artifacts = Path(staging), Path(artifacts)
    archive = staging / f'Unforge-{version}-macos-{architecture}.zip'
    package_app(app, archive)
    verified = dict(receipt)
    verified['notarized'] = False
    verified['notarization'] = 'not-submitted'
    if credentials:
        evidence = notarize_app(app, archive, credentials)
        run('xcrun', 'stapler', 'staple', app)
        run('xcrun', 'stapler', 'validate', app)
        # Only the stapled ticket changes. Never rewrite signed BUILD.json.
        package_app(app, archive)
        verified.update(notarized=True, notarization='accepted-and-stapled', notarySubmission=evidence)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    verified.update(archive=archive.name, archiveSha256=digest)
    external_receipt = staging / f'{archive.stem}.BUILD.json'
    external_receipt.write_text(json.dumps(verified, indent=2) + '\n')
    checksums = staging / 'SHA256SUMS'
    checksums.write_text(f'{digest}  {archive.name}\n')
    output = artifacts / 'Unforge.app'
    final_archive = artifacts / archive.name
    promote_artifacts([(app, output), (archive, final_archive),
                       (external_receipt, artifacts / external_receipt.name),
                       (checksums, artifacts / checksums.name)])
    return output, final_archive, digest, verified


def license_download(url, maximum=512 * 1024):
    # Use macOS's configured certificate trust, without weakening TLS checks.
    result = run('curl', '--proto', '=https', '--fail', '--silent', '--show-error',
                 '--location', '--max-time', '30', '--max-filesize', str(maximum),
                 url, capture_output=True)
    if len(result.stdout) > maximum:
        raise RuntimeError('A runtime license download exceeded its size limit.')
    return result.stdout


def copy_restic_license(destination, version):
    expected = RESTIC_LICENSE_HASHES.get(version)
    if expected is None:
        raise RuntimeError('Pin and review the license for the selected Restic version before packaging.')
    content = license_download(f'https://raw.githubusercontent.com/restic/restic/v{version}/LICENSE', 64 * 1024)
    if hashlib.sha256(content).hexdigest() != expected:
        raise RuntimeError('The Restic license does not match the reviewed release.')
    (destination / 'Restic-LICENSE.txt').write_bytes(content)


def copy_licenses(python, destination):
    """Keep license terms from the exact Python and bootloader used to build."""
    destination.mkdir()
    paths = json.loads(run(python, '-c',
        'import sys,sysconfig,json,importlib.metadata as m; '
        'from pathlib import Path; '
        'd=m.distribution("pyinstaller"); '
        'print(json.dumps({"python":str(Path(sysconfig.get_path("stdlib"))/"LICENSE.txt"),'
        '"version":".".join(str(v) for v in sys.version_info[:3]),'
        '"notices":str(Path(sys.base_prefix)/"Resources/English.lproj/Documentation/_sources/license.rst.txt"),'
        '"bootloader":[str(d.locate_file(f)) for f in d.files '
        'if "license" in str(f).lower() and str(f).endswith((".txt", "LICENSE"))]}))',
        capture_output=True, text=True).stdout)
    python_license = Path(paths['python'])
    if not python_license.is_file() or not paths['bootloader']:
        raise RuntimeError('The build runtime is missing its license files.')
    shutil.copy2(python_license, destination / 'Python-LICENSE.txt')
    # CPython's top-level LICENSE omits notices for bundled extension libraries.
    # The full documentation includes OpenSSL, libffi, zlib and other terms.
    notices = Path(paths['notices'])
    if notices.is_file():
        shutil.copy2(notices, destination / 'Python-THIRD-PARTY-NOTICES.rst')
    else:
        url = f'https://raw.githubusercontent.com/python/cpython/v{paths["version"]}/Doc/license.rst'
        content = license_download(url)
        if len(content) > 512 * 1024 or b'OpenSSL' not in content:
            raise RuntimeError('Could not obtain the full Python runtime license notices.')
        (destination / 'Python-THIRD-PARTY-NOTICES.rst').write_bytes(content)
    for index, source in enumerate(paths['bootloader']):
        shutil.copy2(source, destination / f'PyInstaller-{index}-LICENSE.txt')
    shutil.copy2(ROOT / 'LICENSE', destination / 'Unforge-LICENSE.txt')
    shutil.copy2(ROOT / 'THIRD_PARTY_NOTICES.md', destination / 'Web-THIRD-PARTY-NOTICES.md')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-web-build', action='store_true',
                        help='Use the existing dist; run npm run check before using this option.')
    parser.add_argument('--skip-notarize', action='store_true',
                        help='Sign with Developer ID when available, but do not submit to Apple notary.')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Build the macOS app on a Mac.')
    architecture = platform.machine()
    if architecture not in ('arm64', 'x86_64'):
        parser.error(f'Unsupported build architecture: {architecture}')
    for program in ('xcrun', 'codesign', 'ditto', 'git', 'curl'):
        if not shutil.which(program):
            parser.error(f'{program} is required on the build machine.')
    if not (ROOT / 'macos/Unforge.swift').is_file():
        parser.error('macos/Unforge.swift is missing.')
    version = json.loads((ROOT / 'package.json').read_text())['version']
    if not args.skip_web_build:
        if not (ROOT / 'node_modules').is_dir():
            run('npm', 'ci')
        run('npm', 'run', 'build')
    if not (ROOT / 'dist/index.html').is_file():
        parser.error('The frontend is missing; run npm run build.')
    # Apply the same private-file envelope to native bundles, including dist.
    run(sys.executable, '-c', 'from scripts.package_release import release_files; release_files()')
    inputs = source_identity()
    if json.loads((ROOT / 'package.json').read_text())['version'] != version:
        raise RuntimeError('The source version changed while preparing the build. Start again.')
    if not BUILD_ENV.exists():
        venv.EnvBuilder(with_pip=True).create(BUILD_ENV)
    python = BUILD_ENV / 'bin/python'
    run(python, '-m', 'pip', 'install', '--disable-pip-version-check',
        '-r', ROOT / 'macos/requirements-build.txt')
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    from fetch_restic import fetch, VERSION as RESTIC_VERSION
    restic = fetch(ROOT)
    # A temporary staging directory means a failed build cannot damage the last app.
    with tempfile.TemporaryDirectory(prefix='.build-', dir=ARTIFACTS) as staging_name:
        staging = Path(staging_name)
        app = staging / 'Unforge.app'
        contents = app / 'Contents'
        binaries = contents / 'MacOS'
        resources = contents / 'Resources'
        binaries.mkdir(parents=True)
        resources.mkdir()
        env = {**os.environ, 'MACOSX_DEPLOYMENT_TARGET': '13.0'}
        run(python, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
            '--name', 'unforge-engine', '--target-architecture', architecture,
            '--distpath', staging / 'frozen', '--workpath', staging / 'freeze-work',
            '--specpath', staging, '--paths', ROOT,
            '--add-data', f'{ROOT / "dist"}:dist',
            '--hidden-import', 'agent_jobs', '--hidden-import', 'agent_plans', '--hidden-import', 'lane_review',
            '--hidden-import', 'operations',
            '--hidden-import', 'care', '--hidden-import', 'recovery',
            '--hidden-import', 'projects', '--hidden-import', 'drafts', '--hidden-import', 'runtime',
            '--hidden-import', 'backups', '--hidden-import', 'backup_scheduler',
            '--hidden-import', 'incremental_backups',
            '--hidden-import', 'workspace_watch',
            '--hidden-import', 'build_info', '--hidden-import', 'change_feed',
            '--hidden-import', 'operating', '--hidden-import', 'lanes',
            '--hidden-import', 'integrate', '--hidden-import', 'checks',
            '--hidden-import', 'releases', '--hidden-import', 'app_manifest',
            '--hidden-import', 'exchange', '--hidden-import', 'github_import',
            '--hidden-import', 'second_home',
            '--hidden-import', 'recovery_status',
            '--hidden-import', 'recovery_links',
            '--hidden-import', 'insights', ROOT / 'launcher.py', env=env)
        shutil.copytree(staging / 'frozen/unforge-engine', resources / 'engine', symlinks=True)
        shutil.copy2(restic, resources / 'engine/restic')
        run('xcrun', 'swiftc', '-O', '-target', f'{architecture}-apple-macosx13.0',
            ROOT / 'macos/CloudStatus.swift', '-o', resources / 'engine/unforge-cloud-status', env=env)
        run('xcrun', 'swiftc', '-O', '-target', f'{architecture}-apple-macosx13.0',
            ROOT / 'macos/CloudRehydrate.swift', '-o', resources / 'engine/unforge-cloud-rehydrate', env=env)
        run('xcrun', 'swiftc', '-O', '-target', f'{architecture}-apple-macosx13.0',
            ROOT / 'macos/WorkspaceWatch.swift', '-o', resources / 'engine/unforge-workspace-watch', env=env)
        run('xcrun', 'swiftc', '-O', '-target', f'{architecture}-apple-macosx13.0',
            '-framework', 'AppKit', '-framework', 'WebKit',
            ROOT / 'macos/Unforge.swift', '-o', binaries / 'Unforge', env=env)
        if (ROOT / 'macos/Icon.swift').is_file():
            run('xcrun', 'swiftc', '-O', ROOT / 'macos/Icon.swift', '-o', staging / 'draw-icon')
            run(staging / 'draw-icon', staging / 'Unforge.iconset')
            run('xcrun', 'iconutil', '-c', 'icns', staging / 'Unforge.iconset',
                '-o', resources / 'Unforge.icns')
        info = {
            'CFBundleName': 'Unforge', 'CFBundleDisplayName': 'Unforge',
            'CFBundleIdentifier': 'org.unforge.desktop', 'CFBundleExecutable': 'Unforge',
            'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': version,
            'CFBundleVersion': version, 'LSMinimumSystemVersion': '13.0',
            'LSMultipleInstancesProhibited': True,
            'LSApplicationCategoryType': 'public.app-category.developer-tools',
            'NSHighResolutionCapable': True,
            'NSHumanReadableCopyright': 'Unforge contributors. MIT license.',
            'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
        }
        if (resources / 'Unforge.icns').is_file():
            info['CFBundleIconFile'] = 'Unforge'
        (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
        copy_licenses(python, resources / 'Licenses')
        copy_restic_license(resources / 'Licenses', RESTIC_VERSION)
        shutil.copy2(ROOT / 'docs/MAC_APP.md', resources / 'MAC_APP.md')
        require_source_identity(inputs)
        (resources / 'SOURCE-MANIFEST.json').write_bytes(inputs['manifestBytes'])
        identity = developer_id_identity()
        signing = 'developer-id' if identity else 'ad-hoc'
        if identity and not ENTITLEMENTS.is_file():
            parser.error('macos/Unforge.entitlements is required for Developer ID signing.')
        credentials = None if args.skip_notarize or signing != 'developer-id' else notary_credentials()
        receipt = {'sourceDigest': inputs['sourceDigest'], 'version': version, 'architecture': architecture,
                   'minimumMacOS': '13.0', 'sourceRevision': inputs['sourceRevision'],
                   'sourceDirty': inputs['sourceDirty'],
                   'python': run(python, '--version', capture_output=True, text=True).stdout.strip(),
                   'restic': RESTIC_VERSION,
                   'signing': signing, 'notarized': None if credentials else False,
                   'notarization': 'external-receipt' if credentials else 'not-submitted'}
        (resources / 'BUILD.json').write_text(json.dumps(receipt, indent=2) + '\n')
        if identity:
            sign_app(app, identity, ENTITLEMENTS)
        else:
            run('codesign', '--force', '--deep', '--sign', '-', app)
            run('codesign', '--verify', '--deep', '--strict', '--verbose=2', app)
        output, archive, digest, verified = finish_release(
            app, staging, ARTIFACTS, version, architecture, credentials, receipt)
    print(f'\nApp: {output}\nArchive: {archive}\nSHA256: {digest}')
    if signing == 'developer-id' and verified.get('notarized'):
        print('Developer ID signed, notarized, and stapled.')
    elif signing == 'developer-id':
        print('Developer ID signed with hardened runtime. Notarization skipped (no local notary credentials or --skip-notarize).')
    else:
        print('Ad-hoc signed for local use. Install a Developer ID Application identity to sign for Gatekeeper.')


if __name__ == '__main__':
    main()
