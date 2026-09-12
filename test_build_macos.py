"""Developer ID lookup and Mach-O detection stay local; no account names in fixtures."""
import plistlib
import tempfile
import unittest
from pathlib import Path

from scripts.build_macos import ENTITLEMENTS, is_macho, parse_developer_id_identities


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


if __name__ == '__main__':
    unittest.main()
