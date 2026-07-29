"""Tests for provider-neutral reviewed configuration-item reconciliation."""

import unittest

from src.cmdb.integration_reconciliation import (
    apply_ci_policy,
    apply_ci_type_mappings,
    ci_sync_retry_delay_minutes,
    configuration_catalogue,
    normalize_ci_policy,
    reconcile_configuration_items,
)


def record(external_id: str, name: str, serial: str = "") -> dict:
    """Build one normalized provider configuration for classification tests."""

    return {
        "externalId": external_id,
        "name": name,
        "type": "Server",
        "status": "Active",
        "providerTypeId": "11",
        "providerTypeName": "Server",
        "providerStatusId": "3",
        "providerStatusName": "Managed",
        "fields": {"serialNumber": serial} if serial else {},
        "metadata": {"lifecycle": "in_service"},
        "identifiers": {"serial_number": serial} if serial else {},
    }


class IntegrationReconciliationTests(unittest.TestCase):
    """Keep provider IDs authoritative and mutable names review-only."""

    def test_enrichment_mode_is_explicit_and_invalid_values_use_balanced(self):
        self.assertEqual(normalize_ci_policy(None)["enrichmentMode"], "balanced")
        for mode in ("fast", "balanced", "full"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    normalize_ci_policy({"enrichmentMode": mode})["enrichmentMode"],
                    mode,
                )
        for invalid in ("", "everything", "FULL", 42):
            with self.subTest(invalid=invalid):
                self.assertEqual(
                    normalize_ci_policy({"enrichmentMode": invalid})["enrichmentMode"],
                    "balanced",
                )

    def test_continuous_sync_retry_delay_is_exponential_and_bounded(self):
        """Worker retries should slow repeated failures without going silent."""

        self.assertEqual(ci_sync_retry_delay_minutes(1), 15)
        self.assertEqual(ci_sync_retry_delay_minutes(2), 30)
        self.assertEqual(ci_sync_retry_delay_minutes(3), 60)
        self.assertEqual(ci_sync_retry_delay_minutes(7), 960)
        self.assertEqual(ci_sync_retry_delay_minutes(8), 1440)
        self.assertEqual(ci_sync_retry_delay_minutes(50), 1440)

    def test_existing_mapping_turns_rename_into_an_update(self):
        items = reconcile_configuration_items(
            connection_id="connectwise",
            records=[record("41", "APP-SRV-RENAMED", "SN-41")],
            assets=[
                {
                    "id": "ci-1",
                    "name": "APP-SRV-01",
                    "type": "Server",
                    "status": "Active",
                    "fields": {"serialNumber": "SN-41"},
                    "metadata": {"lifecycle": "in_service"},
                }
            ],
            mappings=[{"externalId": "41", "assetId": "ci-1"}],
        )
        self.assertEqual(items[0]["action"], "update")
        self.assertEqual(items[0]["changedFields"], ["name"])

    def test_field_authority_applies_owned_fields_and_protects_higher_priority_values(self):
        """Provider previews must explain and enforce field-level ownership."""

        items = reconcile_configuration_items(
            connection_id="connectwise",
            provider="connectwise",
            records=[{**record("41", "APP-SRV-RENAMED", "SN-41"), "status": "Retired"}],
            assets=[
                {
                    "id": "ci-1",
                    "name": "APP-SRV-01",
                    "type": "Server",
                    "status": "Active",
                    "source": "ncentral",
                    "fields": {"serialNumber": "SN-41"},
                    "metadata": {
                        "lifecycle": "in_service",
                        "fieldSources": {"name": "connectwise", "status": "ncentral"},
                    },
                }
            ],
            mappings=[{"externalId": "41", "assetId": "ci-1"}],
            field_authority=[
                {
                    "ciType": "Server",
                    "fieldName": "name",
                    "provider": "connectwise",
                    "priority": 20,
                },
                {"ciType": "Server", "fieldName": "name", "provider": "ncentral", "priority": 30},
                {"ciType": "Server", "fieldName": "status", "provider": "ncentral", "priority": 10},
                {
                    "ciType": "Server",
                    "fieldName": "status",
                    "provider": "connectwise",
                    "priority": 20,
                },
            ],
        )

        self.assertEqual(items[0]["action"], "update")
        self.assertEqual(items[0]["changes"], {"name": "APP-SRV-RENAMED"})
        self.assertEqual(items[0]["appliedFields"], ["name"])
        self.assertEqual(items[0]["blockedFields"], ["status"])
        status_decision = next(
            decision for decision in items[0]["fieldDecisions"] if decision["field"] == "status"
        )
        self.assertFalse(status_decision["allowed"])
        self.assertEqual(status_decision["currentProvider"], "ncentral")

    def test_unique_serial_can_link_but_name_only_requires_review(self):
        assets = [
            {
                "id": "ci-1",
                "name": "APP-SRV-01",
                "type": "Server",
                "status": "Active",
                "fields": {"serialNumber": "SN-41"},
                "metadata": {"lifecycle": "in_service"},
            }
        ]
        linked = reconcile_configuration_items(
            connection_id="connectwise",
            records=[record("41", "APP-SRV-01", "SN-41")],
            assets=assets,
            mappings=[],
        )
        conflict = reconcile_configuration_items(
            connection_id="connectwise",
            records=[record("42", "APP-SRV-01")],
            assets=assets,
            mappings=[],
        )
        self.assertEqual(linked[0]["action"], "link")
        self.assertEqual(conflict[0]["action"], "conflict")

    def test_new_and_unchanged_records_are_distinct(self):
        existing = record("41", "APP-SRV-01", "SN-41")
        existing["id"] = "ci-1"
        items = reconcile_configuration_items(
            connection_id="connectwise",
            records=[record("41", "APP-SRV-01", "SN-41"), record("42", "DB-SRV-01")],
            assets=[existing],
            mappings=[{"externalId": "41", "assetId": "ci-1"}],
        )
        self.assertEqual([item["action"] for item in items], ["unchanged", "create"])

    def test_immutable_type_and_status_filters_are_explicit(self):
        included = record("41", "APP-SRV-01")
        excluded_type = {**record("42", "DB-SRV-01"), "providerTypeId": "12"}
        excluded_status = {**record("43", "OLD-SRV-01"), "providerStatusId": "4"}
        policy = normalize_ci_policy(
            {
                "typeMode": "selected",
                "includedTypeIds": ["11"],
                "statusMode": "selected",
                "includedStatusIds": ["3"],
                "excludedExternalIds": [],
            }
        )

        rows, reasons = apply_ci_policy([included, excluded_type, excluded_status], policy)

        self.assertEqual([item["externalId"] for item in rows], ["41"])
        self.assertEqual(reasons, {"type_filtered": 1, "status_filtered": 1})

    def test_catalogue_counts_provider_ids_not_mutable_names(self):
        first = record("41", "APP-SRV-01")
        renamed = {**record("42", "DB-SRV-01"), "providerTypeName": "Managed Server"}

        catalogue = configuration_catalogue([first, renamed])

        self.assertEqual(catalogue["types"], [{"id": "11", "name": "Server", "count": 2}])
        self.assertEqual(catalogue["statuses"][0]["id"], "3")

    def test_type_mapping_uses_immutable_provider_id_and_reports_unmapped_types(self):
        renamed = {**record("41", "APP-SRV-01"), "providerTypeName": "Managed Server"}
        unmapped = {
            **record("42", "SW-01"),
            "providerTypeId": "12",
            "providerTypeName": "Managed Switch",
            "type": "Managed Switch",
        }

        rows, summary = apply_ci_type_mappings(
            [renamed, unmapped],
            {"typeMappings": {"11": "Server"}, "blockUnmappedTypes": False},
        )

        self.assertEqual(rows[0]["type"], "Server")
        self.assertEqual(rows[1]["type"], "Managed Switch")
        self.assertEqual(summary["mapped"], 1)
        self.assertEqual(summary["unmapped"], 1)
        self.assertEqual(
            summary["unmappedTypes"], [{"id": "12", "name": "Managed Switch", "count": 1}]
        )

    def test_strict_type_mapping_turns_unmapped_records_into_conflicts(self):
        rows, summary = apply_ci_type_mappings(
            [record("41", "APP-SRV-01")],
            {"typeMappings": {}, "blockUnmappedTypes": True},
        )
        items = reconcile_configuration_items(
            connection_id="connectwise", records=rows, assets=[], mappings=[]
        )

        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(items[0]["action"], "conflict")
        self.assertIn("not mapped", items[0]["reason"])


if __name__ == "__main__":
    unittest.main()
