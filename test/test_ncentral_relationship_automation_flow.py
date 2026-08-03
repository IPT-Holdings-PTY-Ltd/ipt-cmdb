"""Exercise the guarded N-central relationship observation workflow."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

import backend.main as backend_main
from src.cmdb.repository import StateRepository


class NcentralRelationshipAutomationFlowTests(unittest.TestCase):
    """Verify repeated immutable evidence is required before materialization."""

    def setUp(self) -> None:
        self.state = {
            "companies": [{"id": "acme", "name": "Acme"}],
            "users": [],
            "accessGroups": [],
            "integrations": [
                {
                    "id": "ncentral",
                    "type": "ncentral",
                    "name": "N-central",
                    "revision": 5,
                    "enabled": True,
                    "lifecycleStatus": "active",
                }
            ],
            "syncRuns": [],
            "assets": [
                {"id": "host", "companyId": "acme", "name": "HV01"},
                {"id": "guest", "companyId": "acme", "name": "APP01"},
            ],
            "relationships": [],
            "providerCiMappings": [
                {
                    "id": "mapping-host",
                    "provider": "ncentral",
                    "companyId": "acme",
                    "externalId": "501",
                    "assetId": "host",
                    "providerParentId": "101",
                    "active": True,
                },
                {
                    "id": "mapping-guest",
                    "provider": "ncentral",
                    "companyId": "acme",
                    "externalId": "601",
                    "assetId": "guest",
                    "providerParentId": "101",
                    "active": True,
                },
            ],
            "providerCompanyMappings": [
                {
                    "id": "company-mapping",
                    "provider": "ncentral",
                    "externalId": "101",
                    "companyId": "acme",
                    "active": True,
                }
            ],
            "providerCompanyObservations": [
                {
                    "id": "company-observation",
                    "provider": "ncentral",
                    "externalId": "101",
                    "active": True,
                    "deleted": False,
                }
            ],
            "integrationCiPolicies": [],
        }
        self.repository = StateRepository(self.state, lambda _state: None)
        self.original_repository = backend_main.REPOSITORY
        backend_main.REPOSITORY = self.repository

    def tearDown(self) -> None:
        backend_main.REPOSITORY = self.original_repository

    def test_two_fresh_explicit_observations_auto_approve_one_host_edge(self) -> None:
        observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        records = [
            {
                "externalId": "601",
                "providerObservedAt": observed_at,
                "fields": {
                    "virtualization": {
                        "kind": "virtual_machine",
                        "hostExternalId": "501",
                    }
                },
            }
        ]
        policy = {
            "id": "policy-1",
            "provider": "ncentral",
            "companyId": "acme",
            "providerParentId": "101",
            "revision": 7,
            "relationshipAutomationMode": "auto_explicit",
            "relationshipAutoApproveTypes": ["hosts"],
            "relationshipMinConfidence": 0.98,
            "relationshipMinObservations": 2,
            "relationshipMaxEvidenceAgeHours": 72,
        }
        self.state["integrationCiPolicies"].append(policy)

        first = backend_main._observe_ncentral_relationships(
            "acme",
            records,
            policy,
            "admin",
            provider_company_id="101",
            expected_connection_revision=5,
        )
        self.assertEqual(first["observed"], 1)
        self.assertEqual(first["autoApproved"], 0)
        self.assertEqual(first["reviewRequired"], 1)
        self.assertEqual(self.state["relationships"], [])

        second = backend_main._observe_ncentral_relationships(
            "acme",
            records,
            policy,
            "admin",
            provider_company_id="101",
            expected_connection_revision=5,
        )
        self.assertEqual(second["autoApproved"], 1)
        self.assertEqual(second["reviewRequired"], 0)
        candidate = self.state["ciRelationshipCandidates"][0]
        self.assertEqual(candidate["observationCount"], 2)
        self.assertEqual(candidate["state"], "approved")
        self.assertEqual(len(self.state["relationships"]), 1)
        relationship = self.state["relationships"][0]
        self.assertEqual(relationship["fromId"], "host")
        self.assertEqual(relationship["toId"], "guest")
        self.assertEqual(relationship["type"], "hosts")
        self.assertEqual(relationship["provenance"], "provider")


if __name__ == "__main__":
    unittest.main()
