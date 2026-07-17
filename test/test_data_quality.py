import unittest
from datetime import date

from src.cmdb.data_quality import evaluate_data_quality


class DataQualityTests(unittest.TestCase):
    def setUp(self):
        self.companies = [{"id": "acme", "name": "Acme"}]
        self.assets = [
            {
                "id": "asset-1",
                "companyId": "acme",
                "name": "Sage 200",
                "type": "Business system",
                "source": "ncentral",
                "externalId": None,
                "lastSeen": "2026-01-01T00:00:00Z",
                "metadata": {
                    "lifecycle": "in_service",
                    "operationalStatus": "unknown",
                    "businessOwner": "",
                    "serviceOwner": "",
                    "technicalOwner": "",
                    "rtoHours": "",
                    "rpoHours": "",
                },
            }
        ]

    def test_comprehensive_findings_and_score_are_generated(self):
        result = evaluate_data_quality(self.companies, self.assets, [], today=date(2026, 7, 16))
        keys = {item["ruleKey"] for item in result["findings"]}
        self.assertTrue(
            {
                "missing_owner",
                "missing_business_metadata",
                "unlinked_ci",
                "stale_source",
                "unknown_operational_status",
                "missing_source_identity",
            }.issubset(keys)
        )
        self.assertLess(result["summary"]["score"], 100)
        self.assertEqual(result["customers"][0]["findingCount"], len(result["findings"]))

    def test_active_exception_suppresses_only_matching_finding(self):
        result = evaluate_data_quality(
            self.companies,
            self.assets,
            [],
            [
                {
                    "ruleKey": "missing_owner",
                    "entityId": "asset-1",
                    "state": "active",
                    "expiresAt": "2026-12-31",
                }
            ],
            today=date(2026, 7, 16),
        )
        keys = {item["ruleKey"] for item in result["findings"]}
        self.assertNotIn("missing_owner", keys)
        self.assertIn("stale_source", keys)
        self.assertEqual(result["summary"]["exceptionCount"], 1)

    def test_relationship_and_complete_manual_ci_stay_clean(self):
        asset = {
            "id": "asset-2",
            "companyId": "acme",
            "name": "Switch",
            "type": "Network device",
            "source": "manual",
            "metadata": {
                "lifecycle": "in_service",
                "operationalStatus": "healthy",
                "technicalOwner": "Network Team",
            },
        }
        peer = {
            "id": "asset-3",
            "companyId": "acme",
            "name": "Firewall",
            "type": "Network device",
            "source": "manual",
            "metadata": {
                "lifecycle": "in_service",
                "operationalStatus": "healthy",
                "technicalOwner": "Network Team",
            },
        }
        result = evaluate_data_quality(
            self.companies,
            [asset, peer],
            [{"fromId": "asset-2", "toId": "asset-3"}],
            today=date(2026, 7, 16),
        )
        self.assertEqual(result["summary"]["score"], 100)
        self.assertEqual(result["findings"], [])


if __name__ == "__main__":
    unittest.main()
