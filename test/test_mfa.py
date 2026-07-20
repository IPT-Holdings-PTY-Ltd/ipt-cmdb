import base64
import os
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

import pyotp

from src.cmdb.mfa import (
    MfaConfigurationError,
    decrypt_secret,
    encrypt_secret,
    encryption_key,
    new_totp_secret,
    provisioning_uri,
    qr_data_uri,
    recovery_codes,
    verify_totp,
)


class MfaTests(unittest.TestCase):
    def setUp(self):
        self.key = bytes(range(32))
        self.encoded_key = base64.urlsafe_b64encode(self.key).decode("ascii")

    def test_encryption_key_requires_exactly_256_bits(self):
        self.assertEqual(encryption_key(self.encoded_key), self.key)
        with self.assertRaises(MfaConfigurationError):
            encryption_key("")
        with self.assertRaises(MfaConfigurationError):
            encryption_key(base64.urlsafe_b64encode(b"short").decode("ascii"))

    def test_encryption_key_can_be_loaded_from_secret_file(self):
        with (
            patch.dict(
                os.environ,
                {"MFA_ENCRYPTION_KEY": "", "MFA_ENCRYPTION_KEY_FILE": "/run/secrets/mfa"},
            ),
            patch("src.cmdb.mfa.Path.read_text", return_value=self.encoded_key),
        ):
            self.assertEqual(encryption_key(), self.key)

    def test_totp_seed_is_authenticated_to_its_user(self):
        secret = new_totp_secret()
        encrypted, nonce = encrypt_secret(secret, "user-1", self.key)
        self.assertEqual(decrypt_secret(encrypted, nonce, "user-1", self.key), secret)
        with self.assertRaises(MfaConfigurationError):
            decrypt_secret(encrypted, nonce, "user-2", self.key)

    def test_totp_verification_returns_counter_and_rejects_replay(self):
        secret = new_totp_secret()
        moment = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
        code = pyotp.TOTP(secret).at(moment)
        counter = verify_totp(secret, code, at_time=moment)
        self.assertEqual(counter, int(moment.timestamp()) // 30)
        self.assertIsNone(verify_totp(secret, code, at_time=moment, last_counter=counter))

    def test_provisioning_and_recovery_material_is_interoperable(self):
        secret = new_totp_secret()
        uri = provisioning_uri(secret, "admin@example.com", "IPT CMDB")
        self.assertEqual(pyotp.parse_uri(uri).secret, secret)
        self.assertTrue(qr_data_uri(uri).startswith("data:image/png;base64,"))
        codes = recovery_codes()
        self.assertEqual(len(codes), 10)
        self.assertEqual(len(set(codes)), 10)
        self.assertTrue(all(len(code.replace("-", "")) == 16 for code in codes))


if __name__ == "__main__":
    unittest.main()
