"""Tests for bounded and secret-safe technical inventory extraction."""

from src.cmdb.technical_inventory import technical_inventory_collections


def test_extracts_provider_neutral_collections_and_wraps_role_labels() -> None:
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

    assert len(collections["network_interfaces"]) == 2
    assert collections["server_roles"] == [{"name": "Hyper-V"}, {"name": "Web Server"}]
    assert collections["applications"][0]["displayName"] == "Agent"


def test_removes_sensitive_fields_recursively() -> None:
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
    assert "licenseKey" not in application
    assert "password" not in application["nested"]
    assert application["nested"]["publisher"] == "Vendor"


def test_collection_sizes_are_bounded() -> None:
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

    assert len(collections["network_interfaces"]) == 128
    assert len(collections["applications"]) == 500


def test_explicit_empty_collection_is_retained_to_clear_stale_inventory() -> None:
    collections = technical_inventory_collections(
        {"fields": {"network": {"interfaces": []}, "software": {"applications": []}}}
    )

    assert collections["network_interfaces"] == []
    assert collections["applications"] == []


def test_prefers_rich_normalizer_collections_and_preserves_empty_sections() -> None:
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

    assert collections["hardware"] == {"manufacturer": "Dell"}
    assert collections["network_interfaces"] == []
    assert collections["software"]["items"] == [{"name": "Sage 200", "publisher": "Sage"}]
    assert "unsupported_provider_blob" not in collections
