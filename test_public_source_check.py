"""Public-source privacy rules reject local Fleet state without naming a maintainer."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import check_public_source
from scripts import publish_public_github


class PublicSourceCheckTests(unittest.TestCase):
    def run_check(self, name, contents):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '.git').mkdir()
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
            with (patch.object(check_public_source, 'ROOT', root),
                  patch.object(check_public_source, 'tracked_files', return_value=[Path(name)]),
                  patch.object(check_public_source.subprocess, 'check_output',
                               return_value=b'Viiberr1128 <265590491+Viiberr1128@users.noreply.github.com>')):
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors):
                    return check_public_source.main(), errors.getvalue()

    def test_private_fleet_runtime_path_is_rejected(self):
        private_path = '.codex/skills/' + 'subscription-' + 'fleet/runs/private'
        code, errors = self.run_check('notes.txt', private_path)
        self.assertEqual(code, 1)
        self.assertIn('private Fleet state path', errors)

    def test_private_fleet_state_filename_is_rejected(self):
        code, errors = self.run_check('provider-readiness.json', '{}')
        self.assertEqual(code, 1)
        self.assertIn('private filename', errors)

    def test_generic_public_fleet_adapter_text_is_allowed(self):
        code, errors = self.run_check('docs/fleet.md', 'Use the local fleet executable through a generic adapter.')
        self.assertEqual(code, 0, errors)

    def test_scanner_source_does_not_contain_a_forbidden_runtime_path(self):
        source = Path(check_public_source.__file__).read_bytes()
        self.assertIsNone(check_public_source.PRIVATE_FLEET_STATE.search(source))

    def test_official_publication_requires_the_private_maintainer_check(self):
        def readback(arguments):
            if arguments[-2:] == ['status', '--porcelain']:
                return b''
            if arguments[-3:] == ['remote', 'get-url', 'origin']:
                return b'https://github.com/Viiberr1128/unforge.git\n'
            raise AssertionError(arguments)
        with (patch.object(publish_public_github.subprocess, 'check_output', side_effect=readback),
              patch.object(publish_public_github, 'run') as run,
              patch.dict('os.environ', {}, clear=True)):
            self.assertEqual(publish_public_github.main(), 1)
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
