import unittest

from src.cmdb.repository import StateRepository


class TechnicalInventoryStateRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "companies": [
                {"id": "acme", "name": "Acme", "externalIds": {}},
                {"id": "other", "name": "Other", "externalIds": {}},
            ],
            "users": [],
            "accessGroups": [],
            "integrations": [
                {
                    "id": "ncentral",
                    "type": "ncentral",
                    "revision": 5,
                    "enabled": True,
                    "lifecycleStatus": "active",
                }
            ],
            "syncRuns": [],
            "assets": [
                {"id": "host", "companyId": "acme", "name": "HV01"},
                {"id": "guest", "companyId": "acme", "name": "APP01"},
                {"id": "foreign", "companyId": "other", "name": "OTHER01"},
            ],
            "relationships": [
                {
                    "id": "manual-link",
                    "companyId": "acme",
                    "fromId": "host",
                    "toId": "guest",
                    "type": "hosts",
                    "source": "manual",
                }
            ],
            "providerCiMappings": [
                {
                    "id": "mapping-host",
                    "provider": "ncentral",
                    "companyId": "acme",
                    "externalId": "501",
                    "assetId": "host",
                    "providerParentId": "101",
                    "active": True,
                }
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
            "integrationCiPolicies": [
                {
                    "id": "policy-1",
                    "provider": "ncentral",
                    "companyId": "acme",
                    "providerParentId": "101",
                    "revision": 7,
                    "enabled": True,
                }
            ],
        }
        self.saved = []
        self.repository = StateRepository(self.state, self.saved.append)
        self.relationship_context = {
            "policy_id": "policy-1",
            "expected_policy_revision": 7,
            "expected_connection_revision": 5,
            "provider_parent_id": "101",
        }

    def test_inventory_snapshots_dedupe_retain_history_and_retire_interfaces(self):
        first = {
            "hardware": {"manufacturer": "Dell", "model": "R650"},
            "network_interfaces": [
                {
                    "id": "nic-1",
                    "name": "Ethernet 1",
                    "macAddress": "00:11:22:33:44:55",
                    "ipAddresses": ["10.0.0.10"],
                }
            ],
        }
        self.repository.replace_ci_inventory(
            "ncentral",
            "acme",
            "host",
            "mapping-host",
            {"inventoryCollections": first},
            observed_at="2026-07-31T08:00:00Z",
            retention=2,
        )
        self.repository.replace_ci_inventory(
            "ncentral",
            "acme",
            "host",
            "mapping-host",
            first,
            observed_at="2026-07-31T09:00:00Z",
            retention=2,
        )
        self.assertEqual(
            len(
                [
                    item
                    for item in self.state["ciInventorySnapshots"]
                    if item["collectionType"] == "hardware"
                ]
            ),
            1,
        )
        self.assertEqual(
            self.state["ciInventorySnapshots"][0]["lastObservedAt"],
            "2026-07-31T09:00:00Z",
        )

        for hour, model, interface_id in (
            (10, "R660", "nic-2"),
            (11, "R670", "nic-2"),
        ):
            self.repository.replace_ci_inventory(
                "ncentral",
                "acme",
                "host",
                "mapping-host",
                {
                    "hardware": {"manufacturer": "Dell", "model": model},
                    "network_interfaces": [
                        {
                            "id": interface_id,
                            "name": "Ethernet 2",
                            "ipAddresses": ["10.0.0.11"],
                        }
                    ],
                },
                observed_at=f"2026-07-31T{hour:02}:00:00Z",
                retention=2,
            )

        inventory = self.repository.get_ci_inventory("acme", "host")
        current_hardware = inventory["collections"]["hardware"][0]
        self.assertEqual(current_hardware["payload"]["model"], "R670")
        self.assertEqual(len(inventory["networkInterfaces"]), 1)
        self.assertEqual(inventory["networkInterfaces"][0]["interfaceKey"], "nic-2")
        history = self.repository.get_ci_inventory(
            "acme",
            "host",
            include_history=True,
        )
        self.assertEqual(len(history["collections"]["hardware"]), 2)
        self.assertEqual(len(history["networkInterfaces"]), 2)
        retired = next(
            item for item in history["networkInterfaces"] if item["interfaceKey"] == "nic-1"
        )
        self.assertEqual(retired["retiredAt"], "2026-07-31T10:00:00Z")

    def test_stale_inventory_evidence_never_rolls_back_current_inventory(self):
        self.repository.replace_ci_inventory(
            "ncentral",
            "acme",
            "host",
            "mapping-host",
            {"hardware": {"model": "Current"}},
            observed_at="2026-07-31T10:00:00Z",
        )
        result = self.repository.replace_ci_inventory(
            "ncentral",
            "acme",
            "host",
            "mapping-host",
            {"hardware": {"model": "Older"}},
            observed_at="2026-07-31T09:00:00Z",
        )
        self.assertEqual(result["staleCollections"], ["hardware"])
        current = self.repository.get_ci_inventory("acme", "host")
        self.assertEqual(current["collections"]["hardware"][0]["payload"]["model"], "Current")

    def test_candidate_decisions_are_tenant_safe_and_never_mutate_manual_links(self):
        candidates = self.repository.upsert_relationship_candidates(
            "ncentral",
            "acme",
            "mapping-host",
            [
                {
                    "fromCiId": "host",
                    "toCiId": "guest",
                    "relationshipType": "hosts",
                    "confidence": 0.98,
                    "evidence": {"rule": "explicit_guest_identity"},
                }
            ],
            observed_at="2026-07-31T10:00:00Z",
        )
        self.assertEqual(len(candidates), 1)
        preserved_manual = self.repository.create_relationship(
            {
                "id": "provider-link",
                "fromId": "host",
                "toId": "guest",
                "type": "hosts",
                "sourceMappingId": "mapping-host",
                "confidence": 0.98,
                "evidence": {"rule": "explicit_guest_identity"},
                "provenance": "provider",
            },
            "acme",
        )
        self.assertEqual(preserved_manual["id"], "manual-link")
        self.assertEqual(preserved_manual["provenance"], "manual")
        self.assertIsNone(preserved_manual["sourceMappingId"])
        candidate = self.repository.decide_relationship_candidate(
            "acme",
            candidates[0]["id"],
            "approved",
            notes="Identity verified",
            approved_relationship_id=preserved_manual["id"],
        )
        self.assertEqual(candidate["state"], "approved")
        self.assertEqual(candidate["approvedRelationshipId"], "manual-link")

        self.repository.upsert_relationship_candidates(
            "ncentral",
            "acme",
            "mapping-host",
            [],
            observed_at="2026-07-31T11:00:00Z",
        )
        retired = self.repository.list_relationship_candidates(
            "acme",
            include_retired=True,
        )[0]
        self.assertEqual(retired["state"], "approved")
        self.assertEqual(retired["retiredAt"], "2026-07-31T11:00:00Z")
        self.assertEqual(self.state["relationships"][0]["id"], "manual-link")

        with self.assertRaisesRegex(ValueError, "customer boundary"):
            self.repository.upsert_relationship_candidates(
                "ncentral",
                "acme",
                "mapping-host",
                [
                    {
                        "fromCiId": "host",
                        "toCiId": "foreign",
                        "relationshipType": "connected_to",
                        "confidence": 0.5,
                    }
                ],
            )

    def test_candidate_observation_count_and_revision_ignore_stale_evidence(self):
        candidate_payload = {
            "fromCiId": "host",
            "toCiId": "guest",
            "relationshipType": "hosts",
            "confidence": 0.9,
            "evidence": {"rule": "explicit_guest_identity"},
        }
        first = self.repository.upsert_relationship_candidates(
            "ncentral",
            "acme",
            "mapping-host",
            [candidate_payload],
            observed_at="2026-07-31T10:00:00Z",
        )[0]
        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["observationCount"], 1)

        stale = self.repository.upsert_relationship_candidates(
            "ncentral",
            "acme",
            "mapping-host",
            [{**candidate_payload, "confidence": 0.6}],
            observed_at="2026-07-31T09:00:00Z",
        )[0]
        self.assertEqual(stale["revision"], 1)
        self.assertEqual(stale["observationCount"], 1)
        self.assertEqual(stale["confidence"], 0.9)

        repeated = self.repository.upsert_relationship_candidates(
            "ncentral",
            "acme",
            "mapping-host",
            [candidate_payload],
            observed_at="2026-07-31T11:00:00Z",
        )[0]
        self.assertEqual(repeated["revision"], 2)
        self.assertEqual(repeated["observationCount"], 2)

        self.repository.upsert_relationship_candidates(
            "ncentral",
            "acme",
            "mapping-host",
            [],
            observed_at="2026-07-31T12:00:00Z",
        )
        retired = self.repository.list_relationship_candidates(
            "acme",
            include_retired=True,
        )[0]
        self.assertEqual(retired["revision"], 3)
        self.assertEqual(retired["observationCount"], 2)

    def test_atomic_candidate_approval_guards_revision_and_preserves_manual_link(self):
        candidate = self.repository.upsert_relationship_candidates(
            "ncentral",
            "acme",
            "mapping-host",
            [
                {
                    "fromCiId": "host",
                    "toCiId": "guest",
                    "relationshipType": "hosts",
                    "confidence": 0.98,
                    "evidence": {"impactPolicy": "required"},
                }
            ],
            observed_at="2026-07-31T10:00:00Z",
            **self.relationship_context,
        )[0]

        with self.assertRaisesRegex(ValueError, "changed; refresh"):
            self.repository.approve_relationship_candidate(
                "acme",
                candidate["id"],
                "admin",
                "Reviewed evidence",
                expected_revision=candidate["revision"] + 1,
            )
        self.assertEqual(self.state["ciRelationshipCandidates"][0]["state"], "pending")
        self.assertEqual(len(self.state["relationships"]), 1)

        decision, relationship = self.repository.approve_relationship_candidate(
            "acme",
            candidate["id"],
            "admin",
            "Reviewed evidence",
            expected_revision=candidate["revision"],
        )
        self.assertEqual(relationship["id"], "manual-link")
        self.assertEqual(relationship["provenance"], "manual")
        self.assertIsNone(relationship["sourceMappingId"])
        self.assertEqual(decision["state"], "approved")
        self.assertEqual(decision["revision"], 2)
        self.assertEqual(decision["observationCount"], 1)
        self.assertEqual(decision["approvedRelationshipId"], "manual-link")
        self.assertEqual(len(self.state["relationships"]), 1)

    def test_legacy_provider_candidate_requires_fresh_generation_evidence(self):
        candidate = self.repository.upsert_relationship_candidates(
            "ncentral",
            "acme",
            "mapping-host",
            [
                {
                    "fromCiId": "host",
                    "toCiId": "guest",
                    "relationshipType": "hosts",
                    "confidence": 0.98,
                }
            ],
        )[0]

        with self.assertRaisesRegex(ValueError, "generation evidence is missing"):
            self.repository.approve_relationship_candidate(
                "acme",
                candidate["id"],
                "admin",
                expected_revision=candidate["revision"],
            )
        self.assertEqual(candidate["state"], "pending")
        self.assertEqual(len(self.state["relationships"]), 1)

    def test_atomic_candidate_approval_rolls_back_when_portable_save_fails(self):
        candidate = self.repository.upsert_relationship_candidates(
            "ncentral",
            "acme",
            "mapping-host",
            [
                {
                    "fromCiId": "host",
                    "toCiId": "guest",
                    "relationshipType": "hosts",
                    "confidence": 0.98,
                }
            ],
            observed_at="2026-07-31T10:00:00Z",
            **self.relationship_context,
        )[0]

        def fail_save(_state):
            raise RuntimeError("simulated portable persistence failure")

        repository = StateRepository(self.state, fail_save)
        with self.assertRaisesRegex(RuntimeError, "simulated portable persistence failure"):
            repository.approve_relationship_candidate(
                "acme",
                candidate["id"],
                "admin",
                "Reviewed evidence",
                expected_revision=candidate["revision"],
            )
        stored = self.state["ciRelationshipCandidates"][0]
        self.assertEqual(stored["state"], "pending")
        self.assertEqual(stored["revision"], 1)
        self.assertIsNone(stored["approvedRelationshipId"])
        self.assertEqual(len(self.state["relationships"]), 1)
        self.assertNotIn("provenance", self.state["relationships"][0])

    def test_non_approval_decision_uses_the_same_revision_guard(self):
        candidate = self.repository.upsert_relationship_candidates(
            "ncentral",
            "acme",
            "mapping-host",
            [
                {
                    "fromCiId": "host",
                    "toCiId": "guest",
                    "relationshipType": "hosts",
                    "confidence": 0.98,
                }
            ],
        )[0]
        with self.assertRaisesRegex(ValueError, "changed; refresh"):
            self.repository.decide_relationship_candidate(
                "acme",
                candidate["id"],
                "ignored",
                "admin",
                "Not useful",
                expected_revision=candidate["revision"] + 1,
            )
        ignored = self.repository.decide_relationship_candidate(
            "acme",
            candidate["id"],
            "ignored",
            "admin",
            "Not useful",
            expected_revision=candidate["revision"],
        )
        self.assertEqual(ignored["state"], "ignored")
        self.assertEqual(ignored["revision"], 2)
        self.assertEqual(ignored["observationCount"], 1)

    def test_unresolved_candidate_cannot_be_approved(self):
        candidate = self.repository.upsert_relationship_candidates(
            "ncentral",
            "acme",
            "mapping-host",
            [
                {
                    "fromCiId": "host",
                    "toExternalIdentity": {
                        "provider": "ncentral",
                        "externalId": "guest-501",
                    },
                    "relationshipType": "hosts",
                    "confidence": 0.7,
                }
            ],
        )[0]
        with self.assertRaisesRegex(ValueError, "Resolve both"):
            self.repository.decide_relationship_candidate(
                "acme",
                candidate["id"],
                "approved",
            )

    def test_versioned_evidence_reopens_only_materially_changed_closed_candidates(self):
        for index, decision in enumerate(("ignored", "rejected"), start=1):
            with self.subTest(decision=decision):
                base_hour = (index - 1) * 3 + 1
                relationship_type = f"hosts_{index}"
                original = {
                    "fromCiId": "host",
                    "toCiId": "guest",
                    "relationshipType": relationship_type,
                    "confidence": 0.98,
                    "evidence": {
                        "detector": "ncentral_explicit_virtualization_identity",
                        "evidenceRevision": 1,
                        "evidenceFingerprint": "a" * 64,
                    },
                }
                candidate = self.repository.upsert_relationship_candidates(
                    "ncentral",
                    "acme",
                    "mapping-host",
                    [original],
                    observed_at=f"2026-07-31T{base_hour:02}:00:00Z",
                )[0]
                closed = self.repository.decide_relationship_candidate(
                    "acme",
                    candidate["id"],
                    decision,
                    actor_id="admin",
                    notes="Reviewed provider evidence",
                )

                unchanged = self.repository.upsert_relationship_candidates(
                    "ncentral",
                    "acme",
                    "mapping-host",
                    [original],
                    observed_at=f"2026-07-31T{base_hour + 1:02}:00:00Z",
                )[0]
                self.assertEqual(unchanged["state"], decision)
                self.assertEqual(unchanged["decidedBy"], closed["decidedBy"])
                self.assertEqual(unchanged["decisionNotes"], closed["decisionNotes"])

                changed = self.repository.upsert_relationship_candidates(
                    "ncentral",
                    "acme",
                    "mapping-host",
                    [
                        {
                            **original,
                            "evidence": {
                                **original["evidence"],
                                "evidenceFingerprint": "b" * 64,
                            },
                        }
                    ],
                    observed_at=f"2026-07-31T{base_hour + 2:02}:00:00Z",
                )[0]
                self.assertEqual(changed["state"], "pending")
                self.assertIsNone(changed["decidedBy"])
                self.assertIsNone(changed["decidedAt"])
                self.assertEqual(changed["decisionNotes"], "")


if __name__ == "__main__":
    unittest.main()
