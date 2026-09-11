import argparse
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from Crypto.PublicKey import RSA

import core
import opdash_web


class EncryptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = RSA.generate(1024)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'key.pem'
        self.path.write_bytes(self.key.export_key(pkcs=1))

    def test_private_key_enables_encryption_for_quote_and_trade(self):
        with patch.object(core, 'SysConfig') as config:
            core.configure_futu_encryption(self.path)
        config.set_init_rsa_file.assert_called_once_with(str(self.path.resolve()))
        config.enable_proto_encrypt.assert_called_once_with(True)

    def test_public_key_rejected_before_sdk_configuration(self):
        self.path.write_bytes(self.key.public_key().export_key())
        with patch.object(core, 'SysConfig') as config:
            with self.assertRaisesRegex(ValueError, 'private key'):
                core.configure_futu_encryption(self.path)
        config.enable_proto_encrypt.assert_not_called()

    def test_missing_key_fails_before_network_connections(self):
        with patch.object(core, 'SysConfig') as config:
            with self.assertRaisesRegex(ValueError, 'Cannot load'):
                core.configure_futu_encryption(self.path.with_name('missing.pem'))
        config.set_init_rsa_file.assert_not_called()

    def test_no_key_preserves_launcher_or_embedding_configuration(self):
        with patch.object(core, 'SysConfig') as config:
            core.configure_futu_encryption()
        config.enable_proto_encrypt.assert_not_called()

    def test_cli_overrides_environment_key(self):
        with patch.dict(os.environ, FUTU_RSA_PRIVATE_KEY='environment.pem'):
            parser = argparse.ArgumentParser()
            core.add_dashboard_common_args(parser)
        self.assertEqual(parser.parse_args([]).rsa_private_key, 'environment.pem')
        self.assertEqual(parser.parse_args(['--rsa_private_key', 'explicit.pem']).rsa_private_key,
                         'explicit.pem')

    def test_web_defers_key_loading_until_http_lifespan(self):
        with patch('sys.argv', ['opdash_web.py', '--rsa_private_key', str(self.path)]), \
                patch.object(opdash_web, 'configure_futu_encryption') as configure:
            args = opdash_web.parse_args()
        configure.assert_not_called()
        self.assertEqual(args['rsa_private_key'], str(self.path))
        self.assertIsNone(args['stock_codes'])


if __name__ == '__main__':
    unittest.main()
