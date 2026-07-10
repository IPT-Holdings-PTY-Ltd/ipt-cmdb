import unittest

from src.cmdb.reconciliation import can_apply_field, decide_ci_match


class ReconciliationTests(unittest.TestCase):
    def test_existing_provider_mapping_survives_a_rename(self):
        result = decide_ci_match(
            connection_id="ncentral-acme", external_type="device", external_id="8675309",
            identifiers={"serial_number": "NEW-SERIAL"},
            mappings=[{"connection_id": "ncentral-acme", "external_type": "device", "external_id": "8675309", "ci_id": "ci-1"}],
            identifier_index={},
        )
        self.assertEqual((result.action, result.ci_id), ("mapped", "ci-1"))

    def test_new_provider_uses_unique_serial_not_name(self):
        result = decide_ci_match(
            connection_id="connectwise-acme", external_type="configuration", external_id="45",
            identifiers={"serial_number": "ACME-001"}, mappings=[],
            identifier_index={("serial_number", "ACME-001"): "ci-1"},
        )
        self.assertEqual((result.action, result.ci_id), ("matched", "ci-1"))

    def test_conflicting_identifiers_require_review(self):
        result = decide_ci_match(
            connection_id="ncentral-acme", external_type="device", external_id="45",
            identifiers={"serial_number": "ONE", "device_uuid": "TWO"}, mappings=[],
            identifier_index={("serial_number", "ONE"): "ci-1", ("device_uuid", "TWO"): "ci-2"},
        )
        self.assertEqual(result.action, "review")

    def test_authority_blocks_lower_priority_rename(self):
        priorities = {("ncentral", "display_name"): 10, ("connectwise_manage", "display_name"): 20}
        self.assertFalse(can_apply_field("connectwise_manage", "display_name", priorities, "ncentral"))
