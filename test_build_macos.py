"""Developer ID lookup and Mach-O detection stay local; no account names in fixtures."""
import hashlib
import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

from scripts.build_macos import ENTITLEMENTS, is_macho, parse_developer_id_identities
from scripts import build_macos
import build_info


class BuildMacosSigningTests(unittest.TestCase):
    def test_parse_developer_id_skips_development_and_distribution(self):
        listing = """
  1) AAA "Apple Development: Example (TEAMID)"
  2) BBB "Apple Distribution: Example (TEAMID)"
  3) CCC "Developer ID Application: Example (TEAMID)"
  4) DDD "Developer ID Installer: Example (TEAMID)"
     4 valid identities found
"""
        self.assertEqual(
            parse_developer_id_identities(listing),
            ['Developer ID Application: Example (TEAMID)'],
        )

    def test_parse_developer_id_empty_when_missing(self):
        listing = '  1) AAA "Apple Development: Example (TEAMID)"\n     1 valid identities found\n'
        self.assertEqual(parse_developer_id_identities(listing), [])

    def test_is_macho_detects_64bit_header(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'binary'
            path.write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 16)
            self.assertTrue(is_macho(path))
            path.write_bytes(b'#!/bin/sh\n')
            self.assertFalse(is_macho(path))

    def test_entitlements_are_hardened_runtime_exceptions_only(self):
        data = plistlib.loads(ENTITLEMENTS.read_bytes())
        self.assertTrue(data.get('com.apple.security.cs.allow-jit'))
        self.assertTrue(data.get('com.apple.security.cs.allow-unsigned-executable-memory'))
        self.assertNotIn('com.apple.security.app-sandbox', data)
        self.assertNotIn('com.apple.security.cs.disable-library-validation', data)


class BuildMacosPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='unforge-build-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.artifacts = self.root / 'artifacts'
        self.artifacts.mkdir()
        self.staging = self.artifacts / '.build-test'
        self.staging.mkdir()
        self.app = self.staging / 'Unforge.app'
        self.resources = self.app / 'Contents/Resources'
        self.resources.mkdir(parents=True)
        self.receipt = {'version': '0.3.2', 'sourceRevision': 'a' * 40,
                        'sourceDigest': 'b' * 64, 'sourceDirty': False,
                        'signing': 'developer-id', 'notarized': None,
                        'notarization': 'external-receipt'}
        self.embedded = json.dumps(self.receipt).encode()
        (self.resources / 'BUILD.json').write_bytes(self.embedded)
        self.archive_name = 'Unforge-0.3.2-macos-arm64.zip'
        self.external_name = 'Unforge-0.3.2-macos-arm64.BUILD.json'
        old_app = self.artifacts / 'Unforge.app'
        old_app.mkdir()
        (old_app / 'old').write_bytes(b'previous app')
        self.old = {self.archive_name: b'previous archive', self.external_name: b'previous receipt',
                    'SHA256SUMS': b'previous checksums'}
        for name, content in self.old.items():
            (self.artifacts / name).write_bytes(content)

    def finish(self, credentials=True):
        return build_macos.finish_release(self.app, self.staging, self.artifacts,
                                         '0.3.2', 'arm64', {'example': True} if credentials else None,
                                         self.receipt)

    def assert_previous_release(self):
        self.assertEqual((self.artifacts / 'Unforge.app/old').read_bytes(), b'previous app')
        for name, content in self.old.items():
            self.assertEqual((self.artifacts / name).read_bytes(), content)

    def test_notary_failure_preserves_all_previous_artifacts_and_unsigned_claim(self):
        with patch.object(build_macos, 'notarize_app', side_effect=RuntimeError('Submission rejected')), \
                patch.object(build_macos, 'run') as command:
            with self.assertRaisesRegex(RuntimeError, 'Submission rejected'):
                self.finish()
        command.assert_not_called()
        self.assert_previous_release()
        self.assertEqual((self.resources / 'BUILD.json').read_bytes(), self.embedded)

    def test_staple_failure_never_promotes(self):
        with patch.object(build_macos, 'notarize_app', return_value={'status': 'Accepted'}), \
                patch.object(build_macos, 'run', side_effect=RuntimeError('Stapling failed')):
            with self.assertRaisesRegex(RuntimeError, 'Stapling failed'):
                self.finish()
        self.assert_previous_release()

    def test_verified_receipt_is_external_and_signed_identity_stays_immutable(self):
        def command(*args):
            if args[2] == 'staple':
                (self.resources / 'ticket').write_bytes(b'verified ticket')
            else:
                self.assertEqual(args[2], 'validate')
                self.assertTrue((self.resources / 'ticket').is_file())
        with patch.object(build_macos, 'notarize_app', return_value={'status': 'Accepted', 'submissionId': 'example'}), \
                patch.object(build_macos, 'run', side_effect=command):
            output, archive, digest, receipt = self.finish()
        self.assertEqual((output / 'Contents/Resources/BUILD.json').read_bytes(), self.embedded)
        self.assertTrue(receipt['notarized'])
        self.assertEqual(receipt['notarization'], 'accepted-and-stapled')
        self.assertEqual(receipt['archiveSha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(json.loads((self.artifacts / self.external_name).read_text()), receipt)
        self.assertEqual((self.artifacts / 'SHA256SUMS').read_text(), f'{digest}  {archive.name}\n')
        with zipfile.ZipFile(archive) as packaged:
            self.assertIsNone(json.loads(packaged.read('Unforge.app/Contents/Resources/BUILD.json'))['notarized'])
            self.assertIn('Unforge.app/Contents/Resources/ticket', packaged.namelist())
        self.assertFalse((output / 'old').exists())

    def test_promotion_failure_restores_app_archive_receipt_and_checksum(self):
        replace = Path.replace
        def fail_archive_install(path, target):
            if path == self.staging / self.archive_name and Path(target) == self.artifacts / self.archive_name:
                raise OSError('Interrupted promotion')
            return replace(path, target)
        with patch.object(Path, 'replace', new=fail_archive_install):
            with self.assertRaisesRegex(OSError, 'Interrupted promotion'):
                self.finish(credentials=False)
        self.assert_previous_release()
        self.assertFalse(list(self.artifacts.glob('.previous-release-*')))

    def test_linked_destination_is_refused_without_replacing_other_outputs(self):
        old_app = self.artifacts / 'Unforge.app'
        (old_app / 'old').unlink()
        old_app.rmdir()
        old_app.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'linked release output'):
            self.finish(credentials=False)
        self.assertTrue(old_app.is_symlink())
        for name, content in self.old.items():
            self.assertEqual((self.artifacts / name).read_bytes(), content)

    def test_notary_requires_explicit_accepted_status(self):
        credentials = {'key_path': Path('example.p8'), 'key_id': 'example', 'issuer_id': None}
        rejected = subprocess.CompletedProcess([], 0, stdout=json.dumps({'status': 'Invalid', 'id': 'example'}))
        with patch.object(build_macos, 'run', return_value=rejected):
            with self.assertRaisesRegex(RuntimeError, 'did not accept'):
                build_macos.notarize_app(self.app, self.staging / self.archive_name, credentials)

    def test_source_capture_reports_dirty_inputs_and_refuses_concurrent_changes(self):
        source = self.root / 'source'
        source.mkdir()
        for name in ('package.json', 'package-lock.json', 'vite.config.js', 'index.html', 'engine.py'):
            (source / name).write_text('initial source')
        def git(*args):
            subprocess.run(['git', '-C', str(source), *args], check=True, capture_output=True)
        git('init', '-q')
        git('config', 'user.name', 'Example')
        git('config', 'user.email', 'fixture@unforge.invalid')
        git('add', '.')
        git('commit', '-qm', 'Initial example')
        clean = build_macos.source_identity(source)
        self.assertFalse(clean['sourceDirty'])
        build_macos.require_source_identity(clean, source)
        (source / 'engine.py').write_text('changed source')
        dirty = build_macos.source_identity(source)
        self.assertTrue(dirty['sourceDirty'])
        self.assertEqual(dirty['sourceRevision'], clean['sourceRevision'])
        self.assertNotEqual(dirty['sourceDigest'], clean['sourceDigest'])
        with self.assertRaisesRegex(RuntimeError, 'changed during compilation'):
            build_macos.require_source_identity(clean, source)

    def test_source_and_native_identity_never_borrow_ancestor_repository(self):
        parent = self.root / 'unrelated-owner'; parent.mkdir()
        source = parent / 'extracted-source'; source.mkdir()
        for name in ('package.json', 'package-lock.json', 'vite.config.js', 'index.html', 'build_info.py'):
            (source / name).write_text(json.dumps({'version':'0.3.2'}) if name == 'package.json' else 'Source fixture')
        def git(*args):
            return subprocess.check_output(['git','-C',str(parent),*args], stderr=subprocess.DEVNULL)
        git('init','-q'); git('config','user.name','Fixture')
        git('config','user.email','fixture@unforge.invalid'); git('add','.'); git('commit','-qm','Unrelated fixture')
        build_info.current.cache_clear()
        self.addCleanup(build_info.current.cache_clear)
        with patch.object(build_info, '__file__', str(source / 'build_info.py')):
            running = build_info.current()
        self.assertEqual(running['version'], '0.3.2')
        self.assertIsNone(running['sourceRevision'])
        self.assertIsNone(running['sourceDirty'])
        captured = build_macos.source_identity(source)
        self.assertIsNone(captured['sourceRevision'])
        self.assertIsNone(captured['sourceDirty'])
        self.assertTrue(captured['sourceDigest'])

    def test_source_identity_uses_its_own_repository_and_ignores_injected_git_directory(self):
        source = self.root / 'own-source'; source.mkdir()
        (source / 'package.json').write_text(json.dumps({'version':'0.3.2'}))
        (source / 'build_info.py').write_text('Source fixture')
        def git(*args):
            return subprocess.check_output(['git','-C',str(source),*args], stderr=subprocess.DEVNULL)
        git('init','-q'); git('config','user.name','Fixture')
        git('config','user.email','fixture@unforge.invalid'); git('add','.'); git('commit','-qm','Own source')
        expected = git('rev-parse','HEAD').decode().strip()
        build_info.current.cache_clear()
        self.addCleanup(build_info.current.cache_clear)
        with patch.object(build_info, '__file__', str(source / 'build_info.py')), \
                patch.dict(build_info.os.environ, {'GIT_DIR':str(self.root / 'missing.git')}):
            running = build_info.current()
        self.assertEqual(running['sourceRevision'], expected)
        self.assertFalse(running['sourceDirty'])


if __name__ == '__main__':
    unittest.main()
