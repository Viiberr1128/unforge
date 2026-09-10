"""Publication checks use disposable source trees, never a user's workspace."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import stat
import zipfile
from unittest.mock import patch

from scripts import package_release
from scripts.build_macos import package_app


class ReleasePrivacyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='unforge-publication-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in package_release.ROOT_FILES:
            (self.root / name).write_text('Public example\n')
        (self.root / 'package.json').write_text(json.dumps({'version': '0.3.0'}))
        for name in package_release.DIRECTORIES:
            (self.root / name).mkdir()
        (self.root / 'dist/index.html').write_text('<title>Unforge</title>')
        (self.root / 'docs/example.md').write_text('Public documentation\n')
        self.archive = self.root / 'artifacts/unforge-0.3.0.tar.gz'

    def package(self):
        with patch.object(package_release, 'ROOT', self.root), patch.dict(os.environ, {'SOURCE_DATE_EPOCH': '1234567890'}), contextlib.redirect_stdout(io.StringIO()):
            package_release.main()

    def commit_source(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True, capture_output=True)

    def test_archive_has_no_build_account_metadata_and_rebuild_is_identical(self):
        self.package()
        original = self.archive.read_bytes()
        with tarfile.open(self.archive) as archive:
            members = archive.getmembers()
            self.assertGreater(len(members), 10)
            for item in members:
                self.assertEqual((item.uid, item.gid, item.uname, item.gname), (0, 0, '', ''))
                self.assertEqual(item.mtime, 1234567890)
                self.assertFalse({'atime', 'ctime', 'SCHILY.xattr'} & item.pax_headers.keys())
            self.assertEqual(archive.extractfile('unforge-0.3.0/docs/example.md').read(), b'Public documentation\n')
        os.utime(self.root / 'docs/example.md', (1500000000, 1500000000))
        self.package()
        self.assertEqual(original, self.archive.read_bytes())

    def test_untracked_private_note_is_refused_in_git_checkout(self):
        self.commit_source()
        (self.root / 'docs/my-private-notes.md').write_text('Private account information')
        with self.assertRaisesRegex(SystemExit, 'untracked source file'):
            self.package()
        self.assertFalse(self.archive.exists())

    def test_seeded_credentials_and_database_are_refused_without_git(self):
        for name in ('docs/.env', 'src/auth.json', 'docs/recovery-key', 'docs/customer.sqlite', 'dist/assets.js.map'):
            with self.subTest(name=name):
                path = self.root / name
                path.write_text('Private test fixture')
                try:
                    with self.assertRaisesRegex(SystemExit, 'private state or source maps'):
                        self.package()
                    self.assertFalse(self.archive.exists())
                finally:
                    path.unlink()

    def test_git_source_allows_generated_interface_but_omits_finder_metadata(self):
        self.commit_source()
        (self.root / 'dist/assets.js').write_text('console.log("public")')
        (self.root / 'docs/.DS_Store').write_text('Private Finder metadata')
        self.package()
        with tarfile.open(self.archive) as archive:
            names = archive.getnames()
            self.assertIn('unforge-0.3.0/dist/assets.js', names)
            self.assertFalse(any('.DS_Store' in name for name in names))

    def test_linked_directory_is_refused_before_reading_its_contents(self):
        (self.root / 'docs/private-link').symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaisesRegex(SystemExit, 'linked source directory'):
            self.package()
        self.assertFalse(self.archive.exists())

    def test_mac_archive_preserves_framework_links_without_machine_metadata(self):
        app = self.root / 'Example.app'
        binary = app / 'Contents/MacOS/Example'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'public executable')
        binary.chmod(0o755)
        (app / 'Contents/Current').symlink_to('MacOS', target_is_directory=True)
        (app / '.DS_Store').write_bytes(b'private metadata')
        archive = self.root / 'app.zip'
        package_app(app, archive)
        with zipfile.ZipFile(archive) as result:
            self.assertEqual(set(result.namelist()), {'Example.app/Contents/MacOS/Example', 'Example.app/Contents/Current'})
            link = result.getinfo('Example.app/Contents/Current')
            self.assertTrue(stat.S_ISLNK(link.external_attr >> 16))
            self.assertEqual(result.read(link), b'MacOS')
            self.assertTrue(result.getinfo('Example.app/Contents/MacOS/Example').external_attr >> 16 & 0o111)
            for item in result.infolist():
                self.assertEqual(item.extra, b'')
                self.assertEqual(item.comment, b'')
                self.assertEqual(item.date_time, (1980, 1, 1, 0, 0, 0))

    def test_mac_archive_refuses_links_outside_application(self):
        app = self.root / 'Example.app'
        app.mkdir()
        (app / 'outside').symlink_to(self.root / 'package.json')
        with self.assertRaisesRegex(RuntimeError, 'outside the application'):
            package_app(app, self.root / 'app.zip')


if __name__ == '__main__':
    unittest.main()
