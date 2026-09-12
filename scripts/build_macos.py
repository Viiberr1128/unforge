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
        '--wait', '--timeout', '30m',
    ]
    if credentials['issuer_id']:
        command.extend(['--issuer', credentials['issuer_id']])
    run(*command)


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
            '--hidden-import', 'agent_jobs', '--hidden-import', 'operations',
            '--hidden-import', 'care', '--hidden-import', 'recovery',
            '--hidden-import', 'projects', '--hidden-import', 'drafts', '--hidden-import', 'runtime',
            '--hidden-import', 'backups', '--hidden-import', 'backup_scheduler',
            '--hidden-import', 'incremental_backups',
            '--hidden-import', 'workspace_watch',
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
            'CFBundleVersion': '1', 'LSMinimumSystemVersion': '13.0',
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
        git_revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                                      capture_output=True, text=True)
        source_revision = git_revision.stdout.strip() if git_revision.returncode == 0 else None
        git_status = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT,
                                    capture_output=True, text=True)
        source_files = [p for p in ROOT.glob('*.py') if p.is_file()]
        source_files.extend(ROOT / name for name in ('package.json', 'package-lock.json', 'vite.config.js', 'index.html'))
        for folder in ('src', 'macos', 'scripts', 'dist'):
            source_files.extend(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
        source_manifest = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source_files)}
        source_manifest_bytes = json.dumps(source_manifest, sort_keys=True).encode()
        (resources / 'SOURCE-MANIFEST.json').write_bytes(source_manifest_bytes)
        identity = developer_id_identity()
        signing = 'developer-id' if identity else 'ad-hoc'
        if identity and not ENTITLEMENTS.is_file():
            parser.error('macos/Unforge.entitlements is required for Developer ID signing.')
        credentials = None if args.skip_notarize or signing != 'developer-id' else notary_credentials()
        receipt = {'sourceDigest': hashlib.sha256(source_manifest_bytes).hexdigest(), 'version': version, 'architecture': architecture,
                   'minimumMacOS': '13.0', 'sourceRevision': source_revision,
                   'sourceDirty': bool(git_status.stdout) if git_status.returncode == 0 else None,
                   'python': run(python, '--version', capture_output=True, text=True).stdout.strip(),
                   'restic': RESTIC_VERSION,
                   'signing': signing, 'notarized': bool(credentials)}
        (resources / 'BUILD.json').write_text(json.dumps(receipt, indent=2) + '\n')
        if identity:
            sign_app(app, identity, ENTITLEMENTS)
        else:
            run('codesign', '--force', '--deep', '--sign', '-', app)
            run('codesign', '--verify', '--deep', '--strict', '--verbose=2', app)
        output = ARTIFACTS / 'Unforge.app'
        if output.exists():
            if output.is_symlink():
                raise RuntimeError('Refusing to replace a symlink at the app output path.')
            shutil.rmtree(output)
        shutil.move(app, output)
    archive = ARTIFACTS / f'Unforge-{version}-macos-{architecture}.zip'
    temporary_archive = archive.with_suffix('.zip.tmp')
    package_app(output, temporary_archive)
    temporary_archive.replace(archive)
    if credentials:
        notarize_app(output, archive, credentials)
        run('xcrun', 'stapler', 'staple', output)
        run('xcrun', 'stapler', 'validate', output)
        receipt['notarized'] = True
        package_app(output, temporary_archive)
        temporary_archive.replace(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (ARTIFACTS / 'SHA256SUMS').write_text(f'{digest}  {archive.name}\n')
    print(f'\nApp: {output}\nArchive: {archive}\nSHA256: {digest}')
    if signing == 'developer-id' and receipt.get('notarized'):
        print('Developer ID signed, notarized, and stapled.')
    elif signing == 'developer-id':
        print('Developer ID signed with hardened runtime. Notarization skipped (no local notary credentials or --skip-notarize).')
    else:
        print('Ad-hoc signed for local use. Install a Developer ID Application identity to sign for Gatekeeper.')


if __name__ == '__main__':
    main()
