"""Regression tests for shared credential detection at persistence boundaries."""

import unittest

from src.cmdb.audit import sanitize_audit_value
from src.cmdb.sensitive import (
    canonical_sensitive_key,
    contains_sensitive_data,
    is_sensitive_field_name,
    is_sensitive_scalar,
)


class SensitiveDataTests(unittest.TestCase):
    def test_field_names_are_case_separator_and_nfkc_insensitive(self) -> None:
        fullwidth_api_token = "\uff21\uff30\uff29\uff3f\uff34\uff4f\uff4b\uff45\uff4e"
        self.assertEqual(canonical_sensitive_key(fullwidth_api_token), "apitoken")
        for field in (
            "apiToken",
            "api_key",
            "access-token",
            "Refresh Token",
            "client.secret",
            "passwordHash",
            "credentialsEncrypted",
            "private-key-pem",
            "connection_string",
            "productKey",
        ):
            with self.subTest(field=field):
                self.assertTrue(is_sensitive_field_name(field))

    def test_benign_near_matches_are_retained(self) -> None:
        for field in (
            "apiKeyId",
            "credentialId",
            "passwordPolicy",
            "privateKeyId",
            "secretRotationDate",
            "tokenExpiresAt",
        ):
            with self.subTest(field=field):
                self.assertFalse(is_sensitive_field_name(field))

    def test_strong_scalar_credential_formats_are_detected(self) -> None:
        for value in (
            "Bearer reusable-token",
            "Basic dXNlcjpwYXNzd29yZA==",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEifQ.signature123",
            "-----BEGIN PRIVATE KEY-----\nvalue\n-----END PRIVATE KEY-----",
            "postgresql://user:password@database.example/cmdb",
            "Server=db.example;Password=secret;Database=cmdb",
        ):
            with self.subTest(value=value[:24]):
                self.assertTrue(is_sensitive_scalar(value))
        self.assertFalse(is_sensitive_scalar("https://ncentral.example.com"))
        self.assertFalse(is_sensitive_scalar("Windows Server 2025"))

    def test_recursive_detection_reaches_nested_lists(self) -> None:
        self.assertTrue(contains_sensitive_data({"nested": [[{"apiToken": "secret"}]]}))
        self.assertTrue(contains_sensitive_data({"nested": [["Bearer secret"]]}))
        self.assertFalse(
            contains_sensitive_data(
                {"credentialId": "reference-only", "tokenExpiresAt": "2026-08-04T10:00:00Z"}
            )
        )

    def test_audit_sanitizer_uses_shared_policy_for_keys_and_values(self) -> None:
        sanitized = sanitize_audit_value(
            {
                "api_key": "secret",
                "credentialId": "reference-only",
                "nested": {"header": "Bearer reusable-token"},
            }
        )

        self.assertEqual(sanitized["api_key"], "[redacted]")
        self.assertEqual(sanitized["credentialId"], "reference-only")
        self.assertEqual(sanitized["nested"]["header"], "[redacted]")


if __name__ == "__main__":
    unittest.main()
