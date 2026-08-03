"""Focused safety contracts for bounded provider enrichment cache state."""

from __future__ import annotations

import unittest
from pathlib import Path

from src.cmdb.repository import (
    INTEGRATION_CACHE_MAX_TTL_SECONDS,
    INTEGRATION_CAPABILITY_CACHE_MAX_BYTES,
    _bounded_integration_cache_summary,
    _bounded_integration_cache_ttl,
)

ROOT = Path(__file__).resolve().parents[1]


class IntegrationGraphqlCacheTests(unittest.TestCase):
    """Verify normalized cache payload and migration safety boundaries."""

    def test_summary_is_bounded_fingerprinted_and_independent(self) -> None:
        source = {
            "query": "asset_inventory",
            "available": True,
            "fields": ["systemInfo", "networkInterfaces", "ncentralDevice"],
        }

        summary, fingerprint = _bounded_integration_cache_summary(
            source,
            maximum_bytes=INTEGRATION_CAPABILITY_CACHE_MAX_BYTES,
        )

        self.assertEqual(summary, source)
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")
        source["fields"].append("laterMutation")
        self.assertNotIn("laterMutation", summary["fields"])

    def test_raw_graphql_envelopes_and_credentials_are_rejected(self) -> None:
        unsafe = (
            {"data": {"assetSearch": {"nodes": []}}},
            {"errors": [{"message": "provider error"}]},
            {"wrapper": {"data": {"assetSearch": {"nodes": []}}}},
            {"wrapper": {"errors": [{"message": "provider error"}]}},
            {"metadata": {"authorization": "Bearer do-not-store"}},
            {"configuration": {"apiToken": "do-not-store"}},
            {"configuration": {"x-api-key": "do-not-store"}},
            {"metadata": {"set-cookie": "session=do-not-store"}},
        )

        for value in unsafe:
            with self.subTest(value=list(value)), self.assertRaises(ValueError):
                _bounded_integration_cache_summary(
                    value,
                    maximum_bytes=INTEGRATION_CAPABILITY_CACHE_MAX_BYTES,
                )

    def test_summary_size_depth_and_ttl_are_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "too large"):
            _bounded_integration_cache_summary(
                {"value": "x" * (INTEGRATION_CAPABILITY_CACHE_MAX_BYTES + 1)},
                maximum_bytes=INTEGRATION_CAPABILITY_CACHE_MAX_BYTES,
            )
        nested: dict = {}
        current = nested
        for _index in range(14):
            current["child"] = {}
            current = current["child"]
        with self.assertRaisesRegex(ValueError, "deeply nested"):
            _bounded_integration_cache_summary(
                nested,
                maximum_bytes=INTEGRATION_CAPABILITY_CACHE_MAX_BYTES,
            )
        for ttl in (29, INTEGRATION_CACHE_MAX_TTL_SECONDS + 1, "never"):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                _bounded_integration_cache_ttl(ttl)

    def test_migration_keeps_server_and_device_identity_separate(self) -> None:
        migration = (
            ROOT / "db" / "migrations" / "2026.07.31.2__ncentral_graphql_cache.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("source_server_id varchar(255) NOT NULL", migration)
        self.assertIn("source_device_id varchar(255) NOT NULL", migration)
        self.assertIn("jsonb_typeof(summary) = 'object'", migration)
        self.assertIn("integration_capability_snapshots_expiry_idx", migration)
        self.assertIn("integration_enrichment_previews_expiry_idx", migration)
        self.assertNotIn("graphql_api_token", migration.casefold())


if __name__ == "__main__":
    unittest.main()
