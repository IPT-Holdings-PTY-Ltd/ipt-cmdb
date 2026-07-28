"""Contract tests for reusable integration-provider adapters."""

import unittest

from src.cmdb.integrations import provider_registry
from src.cmdb.integrations.providers.connectwise import ConnectWiseProvider, normalized_policy
from src.cmdb.integrations.providers.ncentral import NcentralProvider


class FakeConnectWiseClient:
    """Return deterministic provider data without network access."""

    def __init__(self, configuration):
        self.configuration = configuration

    def test_connection(self):
        return {"reachable": True, "sampleCompany": {"externalId": "1", "name": "Acme"}}

    def discover_companies(self):
        return [
            {
                "externalId": "1",
                "identifier": "ACME",
                "name": "Acme",
                "status": "Active",
                "type": "Customer",
                "site": "Johannesburg",
                "deleted": False,
            },
            {
                "externalId": "2",
                "identifier": "OLD",
                "name": "Old customer",
                "status": "Inactive",
                "type": "Former customer",
                "site": "Cape Town",
                "deleted": True,
            },
            {
                "externalId": "3",
                "identifier": "BLOCK",
                "name": "Explicit exclusion",
                "status": "Active",
                "type": "Customer",
                "site": "Johannesburg",
                "deleted": False,
            },
        ]

    def list_territories(self, *, limit):
        self.assert_limit = limit
        return ["Cape Town", "Johannesburg", "Unused territory"]


class IntegrationProviderTests(unittest.TestCase):
    """Verify manifests, tests, filtering and read-only preview semantics."""

    def setUp(self):
        self.provider = ConnectWiseProvider(client_factory=FakeConnectWiseClient)

    def test_registry_exposes_input_and_disabled_output_capabilities(self):
        manifest = next(
            item for item in provider_registry.manifests() if item["key"] == "connectwise"
        )
        operations = {item["key"]: item for item in manifest["operations"]}
        self.assertEqual(operations["company.discover"]["direction"], "input")
        self.assertEqual(operations["company.discover"]["status"], "available")
        self.assertEqual(operations["configuration_item.discover"]["status"], "available")
        self.assertEqual(operations["change_ticket.create"]["direction"], "action")
        self.assertEqual(operations["change_ticket.create"]["status"], "disabled")
        self.assertTrue(operations["change_ticket.create"]["requires_approval"])
        ncentral = next(item for item in provider_registry.manifests() if item["key"] == "ncentral")
        ncentral_operations = {item["key"]: item for item in ncentral["operations"]}
        self.assertEqual(ncentral_operations["device.discover"]["status"], "available")
        self.assertEqual(ncentral_operations["device.manage"]["status"], "disabled")
        self.assertTrue(ncentral_operations["device.manage"]["writes_provider"])

    def test_ncentral_adapter_declares_four_read_only_test_stages(self):
        class FakeNcentralClient:
            def __init__(self, configuration):
                self.configuration = configuration

            def test_connection(self):
                return {"reachable": True, "accessibleOrganizations": 2}

        result = NcentralProvider(client_factory=FakeNcentralClient).test_connection({})
        self.assertEqual(len(result["stages"]), 4)
        self.assertFalse(result["writesAttempted"])

    def test_progressive_test_never_attempts_a_provider_write(self):
        result = self.provider.test_connection({"baseUrl": "https://example.invalid"})
        self.assertEqual(len(result["stages"]), 3)
        self.assertTrue(all(item["status"] == "passed" for item in result["stages"]))
        self.assertFalse(result["writesAttempted"])

    def test_policy_is_bounded_and_preview_explains_exclusions(self):
        policy = normalized_policy(
            {
                "includedStatuses": ["Active", "Active", ""],
                "includedTypes": ["Customer"],
                "includedSites": ["Johannesburg"],
                "includeDeleted": False,
                "excludedExternalIds": ["3"],
                "ignored": "not retained",
            }
        )
        preview = self.provider.preview({}, policy)
        options = self.provider.discovery_options({})
        self.assertEqual(policy["includedStatuses"], ["Active"])
        self.assertEqual(preview["discovered"], 3)
        self.assertEqual(preview["included"], 1)
        self.assertEqual(preview["excluded"], 2)
        self.assertEqual(preview["exclusionReasons"]["Deleted in provider"], 1)
        self.assertEqual(preview["exclusionReasons"]["Explicit provider ID exclusion"], 1)
        self.assertTrue(preview["readOnly"])
        self.assertFalse(preview["writesAttempted"])
        self.assertFalse(preview["truncated"])
        self.assertEqual(preview["appliedPolicy"], policy)
        self.assertEqual(options["availableStatuses"], ["Active", "Inactive"])
        self.assertEqual(
            options["availableSites"],
            ["Cape Town", "Johannesburg", "Unused territory"],
        )

    def test_filters_are_or_within_a_menu_and_and_between_menus(self):
        """Multiple values widen one menu while populated menus intersect."""

        type_union = self.provider.preview(
            {},
            {
                "includedTypes": ["Customer", "Former customer"],
                "includeDeleted": True,
            },
        )
        impossible_intersection = self.provider.preview(
            {},
            {
                "includedStatuses": ["Active"],
                "includedSites": ["Cape Town"],
                "includeDeleted": True,
            },
        )
        self.assertEqual(type_union["included"], 3)
        self.assertEqual(impossible_intersection["included"], 0)
        self.assertEqual(impossible_intersection["excluded"], 3)


if __name__ == "__main__":
    unittest.main()
