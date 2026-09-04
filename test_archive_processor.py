#!/usr/bin/env python3
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from archive_processor import (ArchivePasswordError, UnsupportedArchive,
                               passwords_for_source, prepare_archive)


class ArchiveProcessorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.payload = self.root / 'payload'
        self.payload.mkdir()
        (self.payload / 'game.exe').write_bytes(b'game-binary')
        (self.payload / 'README.txt').write_text('keep me')
        (self.payload / 'ryuugames.txt').write_text('remove ad')
        (self.payload / 'promo.url').write_text('remove shortcut')

    def tearDown(self):
        self.tmp.cleanup()

    def _encrypted_zip(self, password='ryuugames.com'):
        source = self.root / 'source.zip'
        subprocess.run([
            '7z', 'a', '-tzip', '-mx=0', f'-p{password}', '-mem=AES256',
            str(source), str(self.payload / '*'),
        ], check=True, capture_output=True, text=True)
        return source

    def test_encrypted_source_becomes_verified_unencrypted_zip(self):
        source = self._encrypted_zip()

        result = prepare_archive(str(source), ['wrong', 'ryuugames.com'])

        output = Path(result['path'])
        self.assertTrue(source.exists())
        self.assertTrue(output.exists())
        self.assertNotEqual(output, source)
        self.assertEqual(result['file_size'], output.stat().st_size)
        self.assertTrue(result['checksum'].startswith('sha256:'))
        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
            self.assertIn('game.exe', names)
            self.assertIn('README.txt', names)
            self.assertNotIn('ryuugames.txt', names)
            self.assertNotIn('promo.url', names)
            self.assertTrue(all(not (item.flag_bits & 0x1) for item in archive.infolist()))
            self.assertEqual(archive.read('game.exe'), b'game-binary')

    def test_delivery_filename_omits_source_and_decryption_labels(self):
        source = self._encrypted_zip()
        result = prepare_archive(
            str(source), ['ryuugames.com'],
            output_name='[Ryuugames] My Game 已解密 decrypted',
        )
        output = Path(result['path'])
        self.assertEqual(output.name, 'My Game.zip')
        self.assertNotRegex(output.name.lower(), r'ryu+games|decrypted|已解密')

    def test_wrong_password_preserves_source_and_creates_no_output(self):
        source = self._encrypted_zip('correct-password')

        with self.assertRaises(ArchivePasswordError):
            prepare_archive(str(source), ['wrong-password'])

        self.assertTrue(source.exists())
        self.assertFalse((self.root / 'source_decrypted.zip').exists())

    def test_non_archive_is_rejected(self):
        source = self.root / 'file.bin'
        source.write_bytes(b'not an archive')
        with self.assertRaises(UnsupportedArchive):
            prepare_archive(str(source), ['anything'])

    def test_source_passwords_are_source_specific_and_configurable(self):
        self.assertIn('ryuugames.com', passwords_for_source('ryuugames', {}))
        self.assertNotIn('ryuugames.com', passwords_for_source('otomi', {}))
        env = {'RYUUGAMES_ARCHIVE_PASSWORDS': 'first, second'}
        self.assertEqual(passwords_for_source('ryuugames', env), ['first', 'second'])


if __name__ == '__main__':
    unittest.main()
