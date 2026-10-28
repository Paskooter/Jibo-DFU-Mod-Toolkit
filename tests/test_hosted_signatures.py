import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fake_flash_server import FakeFlashServer


class SignatureTests(unittest.TestCase):
    def test_large_bounded_signed_payload_is_supported(self):
        import jibo_flash_contract as c
        with FakeFlashServer() as server, patch.object(c, 'trusted_keys', return_value={
                'phoenix-flash-2026-10': server.public_key.read_bytes()}):
            self.assertEqual(c.verify_envelope(server.envelope({'blob': 'x' * 40000}))['blob'], 'x' * 40000)

    def test_missing_crypto_is_dependency_error(self):
        import jibo_flash_contract as c
        with FakeFlashServer() as server, patch.object(c, 'trusted_keys', return_value={
                'phoenix-flash-2026-10': server.public_key.read_bytes()}), \
                patch.object(c, '_python_verifier', return_value=None), \
                patch.object(c, '_openssl_verifier', return_value=None):
            with self.assertRaises(c.ContractError) as caught:
                c.verify_envelope(server.envelope({'schema': 1}))
            self.assertEqual(caught.exception.code, 'dependency')

    def test_exact_ed25519_envelope_verifies_and_tampering_fails(self):
        import jibo_flash_contract as c
        with FakeFlashServer() as server:
            keys = {'phoenix-flash-2026-10': server.public_key.read_bytes()}
            raw = server.envelope({'schema': 1, 'kind': 'synthetic'})
            with patch.object(c, 'trusted_keys', return_value=keys):
                self.assertEqual(c.verify_envelope(raw)['kind'], 'synthetic')
                with self.assertRaises(c.ContractError) as caught:
                    c.verify_envelope(server.envelope({'schema': 1}, bad_signature=True))
                self.assertEqual(caught.exception.code, 'signature')

    def test_signed_duplicate_keys_are_rejected(self):
        import jibo_flash_contract as c
        with FakeFlashServer() as server, patch.object(c, 'trusted_keys', return_value={
                'phoenix-flash-2026-10': server.public_key.read_bytes()}):
            with self.assertRaises(c.ContractError):
                c.verify_envelope(server.envelope(b'{"schema":1,"schema":1}'))

    def test_unsafe_json_floats_and_control_strings_are_rejected(self):
        import jibo_flash_contract as c
        for raw in (b'{"size":1.0}', b'{"size":NaN}', b'{"notes":"\\u001b"}',
                    b'{"schema":1,"schema":2}'):
            with self.subTest(raw=raw), self.assertRaises(c.ContractError):
                c.strict_json(raw)

    def test_test_only_trust_cannot_be_used_as_production_trust(self):
        import jibo_flash_contract as c
        with tempfile.TemporaryDirectory() as temp, patch.object(c, 'ASSETS', Path(temp)):
            (Path(temp) / 'trust.json').write_text(json.dumps({
                'schema': 1, 'test_only': True, 'keys': {}}))
            with self.assertRaises(c.ContractError) as caught:
                c.trusted_keys()
            self.assertEqual(caught.exception.code, 'dependency')

    def test_shipped_trust_pins_the_production_public_key(self):
        import hashlib
        import jibo_flash_contract as c
        trust = json.loads((c.ASSETS / 'trust.json').read_text())
        self.assertIs(trust['test_only'], False)
        self.assertEqual(set(c.trusted_keys()), {'phoenix-flash-2026-10'})
        public = c.trusted_keys()['phoenix-flash-2026-10']
        self.assertEqual(hashlib.sha256(public).hexdigest(),
                         'e4af88d9b3fda8533f167c6b66dfd591cf0c8ba0b7885de84082baa575eaca53')
        self.assertEqual(c.CONTRACT['public_key'].encode('ascii'), public)

    def test_shipped_toolkit_version_is_1_0_4(self):
        import jibo_flash_contract as c
        self.assertEqual(c.TOOLKIT_VERSION, '1.0.4')

    def test_release_build_refuses_a_test_signed_index(self):
        import jibo_flash_contract as c
        with FakeFlashServer() as server:
            raw = server.envelope({'schema': 1, 'kind': 'phoenix-flash-index-v1'})
            with self.assertRaises(c.ContractError) as caught:
                c.verify_envelope(raw, 'index')
            self.assertEqual(caught.exception.code, 'signature')

    def test_openssl_verifies_positive_and_negative_without_python_crypto(self):
        import jibo_flash_contract as c
        with FakeFlashServer() as server, patch.object(c, '_python_verifier', return_value=None):
            keys = {'phoenix-flash-2026-10': server.public_key.read_bytes()}
            with patch.object(c, 'trusted_keys', return_value=keys):
                self.assertEqual(c.verify_envelope(server.envelope({'value': 3}))['value'], 3)
                with self.assertRaises(c.ContractError):
                    c.verify_envelope(server.envelope({'value': 3}, bad_signature=True))


if __name__ == '__main__':
    unittest.main()
