"""Tests for bounded and secret-safe technical inventory extraction."""

import unittest

from src.cmdb.technical_inventory import technical_inventory_collections


class TechnicalInventoryTests(unittest.TestCase):
    def test_extracts_provider_neutral_collections_and_wraps_role_labels(self) -> None:
        collections = technical_inventory_collections(
            {
                "fields": {
                    "hardware": {
                        "memoryModules": [{"capacityBytes": 1024, "location": "DIMM 1"}],
                        "physicalDisks": [{"model": "Disk", "capacityBytes": 2048}],
                    },
                    "network": {
                        "interfaces": [
                            {"description": "Ethernet", "macAddress": "AA:BB"},
                            {"description": "Wi-Fi", "ipAddresses": ["10.0.0.10"]},
                        ]
                    },
                    "software": {
                        "applications": [{"displayName": "Agent", "version": "1.0"}],
                        "roles": ["Hyper-V", "Web Server"],
                    },
                }
            }
        )

        self.assertEqual(len(collections["network_interfaces"]), 2)
        self.assertEqual(
            collections["server_roles"],
            [{"name": "Hyper-V"}, {"name": "Web Server"}],
        )
        self.assertEqual(collections["applications"][0]["displayName"], "Agent")

    def test_removes_sensitive_fields_recursively(self) -> None:
        collections = technical_inventory_collections(
            {
                "fields": {
                    "software": {
                        "applications": [
                            {
                                "displayName": "Licensed product",
                                "licenseKey": "SECRET",
                                "nested": {"password": "SECRET", "publisher": "Vendor"},
                            }
                        ]
                    }
                }
            }
        )

        application = collections["applications"][0]
        self.assertNotIn("licenseKey", application)
        self.assertNotIn("password", application["nested"])
        self.assertEqual(application["nested"]["publisher"], "Vendor")

    def test_removes_key_variants_nested_lists_and_credential_scalars(self) -> None:
        collections = technical_inventory_collections(
            {
                "inventoryCollections": {
                    "hardware": {
                        "manufacturer": "Dell",
                        "apiToken": "token-a",
                        "api_key": "key-b",
                        "access-token": "token-c",
                        "Refresh Token": "token-d",
                        "client.secret": "secret-e",
                        "passwordHash": "hash-f",
                        "productKey": "key-g",
                        "credentialId": "safe-reference-id",
                        "tokenExpiresAt": "2026-08-04T10:00:00Z",
                        "nested": [
                            [
                                {
                                    "name": "safe nested row",
                                    "privateKey": "private-h",
                                    "header": "Bearer reusable-token",
                                }
                            ]
                        ],
                    }
                }
            }
        )

        hardware = collections["hardware"]
        for field in (
            "apiToken",
            "api_key",
            "access-token",
            "Refresh Token",
            "client.secret",
            "passwordHash",
            "productKey",
        ):
            self.assertNotIn(field, hardware)
        self.assertEqual(hardware["credentialId"], "safe-reference-id")
        self.assertEqual(hardware["tokenExpiresAt"], "2026-08-04T10:00:00Z")
        self.assertEqual(hardware["nested"], [[{"name": "safe nested row"}]])

    def test_collection_sizes_are_bounded(self) -> None:
        collections = technical_inventory_collections(
            {
                "fields": {
                    "network": {
                        "interfaces": [{"index": index} for index in range(200)],
                    },
                    "software": {
                        "applications": [{"name": str(index)} for index in range(700)],
                    },
                }
            }
        )

        self.assertEqual(len(collections["network_interfaces"]), 128)
        self.assertEqual(len(collections["applications"]), 500)

    def test_explicit_empty_collection_is_retained_to_clear_stale_inventory(self) -> None:
        collections = technical_inventory_collections(
            {"fields": {"network": {"interfaces": []}, "software": {"applications": []}}}
        )

        self.assertEqual(collections["network_interfaces"], [])
        self.assertEqual(collections["applications"], [])

    def test_prefers_rich_normalizer_collections_and_preserves_empty_sections(self) -> None:
        collections = technical_inventory_collections(
            {
                "inventoryCollections": {
                    "hardware": {"manufacturer": "Dell", "password": "discard"},
                    "network_interfaces": [],
                    "software": {
                        "count": 1,
                        "items": [
                            {
                                "name": "Sage 200",
                                "publisher": "Sage",
                                "licenseKey": "discard",
                            }
                        ],
                    },
                    "unsupported_provider_blob": {"secret": "discard"},
                },
                "fields": {"network": {"interfaces": [{"name": "legacy"}]}},
            }
        )

        self.assertEqual(collections["hardware"], {"manufacturer": "Dell"})
        self.assertEqual(collections["network_interfaces"], [])
        self.assertEqual(
            collections["software"]["items"],
            [{"name": "Sage 200", "publisher": "Sage"}],
        )
        self.assertNotIn("unsupported_provider_blob", collections)


if __name__ == "__main__":
    unittest.main()
