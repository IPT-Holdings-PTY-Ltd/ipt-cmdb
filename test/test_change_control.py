import unittest
from copy import deepcopy
from io import BytesIO

from pypdf import PdfReader

from src.cmdb.change_control import (
    build_impact_snapshot,
    create_change_record,
    derive_change_approvers,
    initial_closure_assessment,
    normalise_change_payload,
    preview_change_impact,
    reassign_change_record,
    record_external_approval,
    render_change_pdf,
    transition_change_record,
    update_change_record,
)

ASSETS = [
    {
        "id": "db",
        "companyId": "acme",
        "name": "DB01",
        "type": "Server",
        "source": "ncentral",
        "metadata": {
            "criticality": "critical",
            "environment": "production",
            "technicalOwner": "Platform Team",
            "site": "HQ",
        },
    },
    {
        "id": "app",
        "companyId": "acme",
        "name": "APP01",
        "type": "Server",
        "source": "ncentral",
        "metadata": {
            "criticality": "high",
            "environment": "production",
            "serviceOwner": "Applications",
        },
    },
    {
        "id": "portal",
        "companyId": "acme",
        "name": "Customer Portal",
        "type": "Software",
        "source": "manual",
        "metadata": {"criticality": "high", "environment": "production"},
    },
    {
        "id": "sage",
        "companyId": "acme",
        "name": "Sage 200",
        "type": "Business system",
        "source": "manual",
        "metadata": {
            "criticality": "critical",
            "environment": "production",
            "businessOwner": "Finance Director",
            "signoffDelegate": "Financial Controller",
            "signoffRequired": "yes",
            "department": "Finance",
            "userPopulation": "42 users",
            "rtoHours": "4",
            "rpoHours": "1",
        },
        "responsibilities": [
            {
                "contactId": "finance-contact",
                "contactName": "Financial Controller",
                "contactEmail": "controller@example.com",
                "role": "signoff_delegate",
                "isPrimary": True,
                "effectiveUntil": None,
            }
        ],
    },
    {
        "id": "other",
        "companyId": "northwind",
        "name": "OTHER",
        "type": "Server",
        "source": "ncentral",
        "metadata": {},
    },
]

RELATIONSHIPS = [
    {"fromId": "app", "toId": "db", "type": "depends_on"},
    {"fromId": "portal", "toId": "app", "type": "installed_on"},
    {"fromId": "sage", "toId": "app", "type": "depends_on", "impactPolicy": "required"},
]


class ChangeControlTests(unittest.TestCase):
    def test_impact_snapshot_follows_supporting_to_affected_semantics(self):
        snapshot = build_impact_snapshot("acme", ["db"], ASSETS, RELATIONSHIPS)
        self.assertEqual([item["assetId"] for item in snapshot], ["db", "app", "portal", "sage"])
        self.assertEqual(snapshot[1]["role"], "Direct impact")
        self.assertEqual(snapshot[2]["role"], "Downstream impact")
        self.assertEqual(snapshot[2]["relationshipPath"], ["depends_on", "installed_on"])
        self.assertEqual(snapshot[3]["impactSeverity"], "outage")

    def test_impact_preview_flags_risk_and_missing_owners(self):
        preview = preview_change_impact("acme", ["db"], True, ASSETS, RELATIONSHIPS)
        self.assertEqual(preview["summary"]["scopeCount"], 1)
        self.assertEqual(preview["summary"]["missingOwnerCount"], 1)
        self.assertEqual(preview["summary"]["businessSystemCount"], 1)
        self.assertEqual(preview["summary"]["businessOwners"], ["Finance Director"])
        self.assertIn(preview["summary"]["suggestedRisk"]["level"], {"high", "critical"})

    def test_cross_customer_scope_is_rejected(self):
        with self.assertRaises(ValueError):
            build_impact_snapshot("acme", ["other"], ASSETS, RELATIONSHIPS)

    def test_change_record_has_connectwise_ready_external_envelope(self):
        change = self._change()
        self.assertEqual(change["integrationState"]["connectwise"]["status"], "not_published")
        self.assertEqual(change["revision"], 1)
        self.assertEqual(len(change["impactSnapshot"]), 4)
        self.assertEqual(change["statusHistory"][0]["toStatus"], "draft")

    def test_approval_plan_uses_structured_business_system_delegate(self):
        plan = derive_change_approvers(self._change())
        self.assertEqual(plan["missing"], [])
        self.assertEqual(len(plan["approvers"]), 1)
        self.assertEqual(plan["approvers"][0]["approverEmail"], "controller@example.com")
        self.assertEqual(plan["approvers"][0]["responsibilityRoles"], ["signoff_delegate"])
        self.assertEqual(plan["approvers"][0]["scope"][0]["name"], "Sage 200")

    def test_approval_plan_reports_required_system_without_deliverable_owner(self):
        change = self._change()
        change["impactSummary"]["businessSystems"][0]["responsibilities"] = []
        plan = derive_change_approvers(change)
        self.assertEqual(plan["approvers"], [])
        self.assertEqual(plan["missing"][0]["name"], "Sage 200")

    def test_external_approval_evidence_completes_change_when_batch_is_complete(self):
        actor = {"id": "operator", "email": "operator@example.com"}
        change = self._change()
        change = transition_change_record(change, "impact_review", {}, actor)
        change = transition_change_record(change, "awaiting_approval", {}, actor)
        updated = record_external_approval(
            change,
            {
                "id": "request-1",
                "batchId": "batch-1",
                "approverName": "Financial Controller",
                "approverEmail": "controller@example.com",
                "approverContactId": "finance-contact",
                "responsibilityRole": "signoff_delegate",
                "scope": [{"type": "business_system", "id": "sage", "name": "Sage 200"}],
                "decidedAt": "2026-07-15T10:00:00Z",
            },
            "approved",
            "Approved for the maintenance window",
            True,
        )
        self.assertEqual(updated["status"], "approved")
        self.assertEqual(updated["approvals"][-1]["actorEmail"], "controller@example.com")
        self.assertEqual(updated["statusHistory"][-1]["toStatus"], "approved")

    def test_edit_creates_revision_and_refreshes_impact(self):
        change = self._change()
        updated = update_change_record(
            change,
            {
                "expectedRevision": 1,
                "title": "Database maintenance revised",
                "scopeAssetIds": ["app"],
            },
            {"id": "operator", "email": "operator@example.com"},
            ASSETS,
            RELATIONSHIPS,
        )
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["title"], "Database maintenance revised")
        self.assertEqual(updated["scopeAssetIds"], ["app"])
        self.assertEqual(updated["lastUpdatedBy"]["email"], "operator@example.com")

    def test_change_enums_are_trimmed_and_schedule_communication_is_validated(self):
        values = normalise_change_payload(
            {
                **self._change(),
                "changeType": " Normal ",
                "category": " Database ",
                "priority": " High ",
                "riskLevel": " Medium ",
                "communicationStatus": " Required ",
            }
        )
        self.assertEqual(values["changeType"], "normal")
        self.assertEqual(values["category"], "database")
        self.assertEqual(values["priority"], "high")
        self.assertEqual(values["riskLevel"], "medium")
        self.assertEqual(values["communicationStatus"], "required")

        approved = {**self._change(), "status": "approved"}
        updated = update_change_record(
            approved,
            {"expectedRevision": 1, "communicationStatus": " Completed "},
            {"id": "operator", "email": "operator@example.com"},
            ASSETS,
            RELATIONSHIPS,
        )
        self.assertEqual(updated["communicationStatus"], "completed")
        with self.assertRaisesRegex(ValueError, "valid communicationStatus"):
            update_change_record(
                approved,
                {"expectedRevision": 1, "communicationStatus": "waiting"},
                {"id": "operator", "email": "operator@example.com"},
                ASSETS,
                RELATIONSHIPS,
            )

    def test_reassignment_creates_immutable_identity_history(self):
        change = self._change()
        actor = {"id": "admin", "email": "admin@example.com", "displayName": "Admin"}
        assignee = {
            "id": "operator",
            "email": "operator@example.com",
            "displayName": "Operations Tech",
        }
        updated = reassign_change_record(
            change,
            assignee,
            "Move this change to the on-call technician",
            actor,
        )
        self.assertEqual(updated["assignedUserId"], "operator")
        self.assertEqual(updated["assignedTechnician"], "Operations Tech")
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(
            updated["assignmentHistory"][-1]["previousDisplayName"], "tech@example.com"
        )
        self.assertEqual(updated["assignmentHistory"][-1]["assignedUserId"], "operator")
        self.assertEqual(
            updated["assignmentHistory"][-1]["reason"],
            "Move this change to the on-call technician",
        )
        with self.assertRaisesRegex(ValueError, "different technician"):
            reassign_change_record(updated, assignee, "Try the same person again", actor)

    def test_assignment_changes_require_dedicated_action_and_active_change(self):
        change = self._change()
        with self.assertRaisesRegex(ValueError, "dedicated reassignment"):
            update_change_record(
                change,
                {"expectedRevision": 1, "assignedTechnician": "other@example.com"},
                {"id": "admin", "email": "admin@example.com"},
                ASSETS,
                RELATIONSHIPS,
            )
        closed = {**change, "status": "closed"}
        with self.assertRaisesRegex(ValueError, "cannot be reassigned"):
            reassign_change_record(
                closed,
                None,
                "The work is already closed",
                {"id": "admin", "email": "admin@example.com"},
            )

    def test_approval_and_failure_transitions_are_distinct_and_auditable(self):
        actor = {"id": "operator", "email": "operator@example.com"}
        change = self._change()
        for status in (
            "impact_review",
            "awaiting_approval",
            "approved",
            "scheduled",
            "implementing",
        ):
            change = transition_change_record(
                change, status, {"reason": f"Move to {status}"}, actor
            )
        self.assertEqual(change["approvals"][-1]["decision"], "approved")
        change = transition_change_record(
            change,
            "failed",
            {
                "reason": "Database service did not recover",
                "actualOutageMinutes": 18,
                "validationResult": "Health check failed",
            },
            actor,
        )
        self.assertEqual(change["outcome"], "failed")
        self.assertEqual(change["actualOutageMinutes"], 18)
        change = transition_change_record(
            change,
            "backed_out",
            {"reason": "Snapshot restored", "rollbackResult": "Service recovered"},
            actor,
        )
        self.assertTrue(change["rollbackExecuted"])
        self.assertEqual(change["outcome"], "backed_out")
        self.assertEqual(change["statusHistory"][-1]["toStatus"], "backed_out")

    def test_invalid_transition_is_rejected(self):
        with self.assertRaises(ValueError):
            transition_change_record(
                self._change(),
                "completed",
                {"reason": "Skipped controls"},
                {"id": "admin", "email": "admin@example.com"},
            )

    def test_post_change_closure_enforces_tests_pir_and_signoff(self):
        actor = {"id": "operator", "email": "operator@example.com"}
        change = self._change()
        self.assertEqual(change["closureAssessment"]["tests"][0]["key"], "validation_plan")
        for status in (
            "impact_review",
            "awaiting_approval",
            "approved",
            "scheduled",
            "implementing",
        ):
            change = transition_change_record(
                change,
                status,
                {"reason": f"Move to {status}"},
                actor,
            )
        change = transition_change_record(
            change,
            "completed",
            {
                "reason": "Implementation completed",
                "actualOutageMinutes": 8,
                "validationResult": "Initial service health checks passed",
            },
            actor,
        )
        change = transition_change_record(
            change,
            "post_implementation_review",
            {"reason": "Begin final review"},
            actor,
        )
        incomplete = deepcopy(change["closureAssessment"])
        incomplete["closureSummary"] = "Completed successfully"
        with self.assertRaisesRegex(ValueError, "Complete the required test"):
            transition_change_record(
                change,
                "closed",
                {
                    "reason": "Completed successfully",
                    "closureAssessment": incomplete,
                },
                actor,
            )

        assessment = deepcopy(change["closureAssessment"])
        assessment.update(
            {
                "pirCompleted": True,
                "lessonsLearned": "The maintenance window was sufficient.",
                "stakeholderConfirmation": "confirmed",
                "closureSummary": "Database maintenance completed and service was restored.",
            }
        )
        for test in assessment["tests"]:
            test["result"] = "passed"
            test["actualResult"] = "Database health and portal login checks passed."
            test["evidence"] = "Monitoring event EVT-100"
        closed = transition_change_record(
            change,
            "closed",
            {
                "reason": assessment["closureSummary"],
                "closureAssessment": assessment,
            },
            actor,
        )
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["outcome"], "successful")
        self.assertTrue(closed["closureAssessment"]["pirRequired"])
        self.assertEqual(
            closed["closureAssessment"]["tests"][0]["testedBy"],
            "operator@example.com",
        )
        self.assertEqual(closed["closureNotes"], assessment["closureSummary"])

    def test_closure_template_tokens_are_case_insensitive(self):
        change = self._change()
        change["templateSnapshot"] = {
            "content": {
                "closureTests": [
                    {
                        "key": "service_check",
                        "label": "{{Service_Test}}",
                        "expectedResultTemplate": "{{Service_Test}} succeeds on {{Asset_Name}}",
                        "required": True,
                        "evidenceRequired": False,
                    }
                ]
            }
        }
        change["templateParameters"] = {"service_test": "Portal login"}

        assessment = initial_closure_assessment(change)
        self.assertEqual(assessment["tests"][0]["label"], "Portal login")
        self.assertEqual(
            assessment["tests"][0]["expectedResult"],
            "Portal login succeeds on DB01",
        )

    def test_closed_change_pdf_includes_structured_validation_evidence(self):
        change = self._change()
        change["outcome"] = "successful_with_issues"
        change["closureNotes"] = "Service restored with a monitoring follow-up."
        change["closureAssessment"] = {
            **change["closureAssessment"],
            "implementationResult": "successful_with_issues",
            "serviceStatus": "restored",
            "pirRequired": True,
            "pirCompleted": True,
            "lessonsLearned": "Extend monitoring observation for database maintenance.",
            "stakeholderConfirmation": "confirmed",
            "closureSummary": "Service restored with a monitoring follow-up.",
            "closedBy": "operator@example.com",
            "closedAt": "2026-07-15T21:10:00Z",
            "tests": [
                {
                    **change["closureAssessment"]["tests"][0],
                    "result": "passed",
                    "actualResult": "Portal login succeeded.",
                    "evidence": "Monitoring event EVT-100",
                    "testedBy": "operator@example.com",
                    "testedAt": "2026-07-15T21:05:00Z",
                }
            ],
        }
        reader = PdfReader(
            BytesIO(
                render_change_pdf(
                    change,
                    {"id": "acme", "name": "Acme Manufacturing"},
                    {"name": "CMDB Hub", "accent": "#4cc7b1"},
                )
            )
        )
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertIn("Post-change review and closure", text)
        self.assertIn("Portal login succeeded", text)
        self.assertIn("Monitoring event EVT-100", text)

    def test_informational_relationship_does_not_propagate_impact(self):
        relationships = [
            {
                "fromId": "sage",
                "toId": "db",
                "type": "depends_on",
                "impactPolicy": "informational",
            },
        ]
        snapshot = build_impact_snapshot("acme", ["db"], ASSETS, relationships)
        self.assertEqual([item["assetId"] for item in snapshot], ["db"])

    def test_single_host_change_uses_cluster_ha_capacity(self):
        assets, relationships = self._virtualization_fixture()
        preview = preview_change_impact("acme", ["host-1"], False, assets, relationships)
        by_id = {item["assetId"]: item for item in preview["items"]}
        self.assertEqual(by_id["vm-1"]["impactSeverity"], "protected")
        self.assertEqual(by_id["erp"]["impactSeverity"], "protected")
        self.assertIn("1 eligible host", by_id["vm-1"]["virtualizationDecision"])
        self.assertEqual(preview["summary"]["protectedVmCount"], 1)
        self.assertIn(
            "protected by verified HA capacity",
            " ".join(preview["summary"]["suggestedRisk"]["factors"]),
        )

    def test_multi_host_change_exhausts_cluster_failover(self):
        assets, relationships = self._virtualization_fixture()
        preview = preview_change_impact("acme", ["host-1", "host-2"], False, assets, relationships)
        by_id = {item["assetId"]: item for item in preview["items"]}
        self.assertEqual(by_id["vm-1"]["impactSeverity"], "outage")
        self.assertEqual(by_id["erp"]["impactSeverity"], "outage")
        self.assertEqual(preview["summary"]["outageVmCount"], 1)

    def test_shared_datastore_failure_bypasses_compute_ha(self):
        assets, relationships = self._virtualization_fixture()
        preview = preview_change_impact("acme", ["ds-1"], False, assets, relationships)
        by_id = {item["assetId"]: item for item in preview["items"]}
        self.assertEqual(by_id["vm-1"]["impactSeverity"], "outage")
        self.assertEqual(by_id["erp"]["impactSeverity"], "outage")

    def test_pdf_contains_change_and_impact_details(self):
        change = self._change()
        pdf = render_change_pdf(
            change,
            {"id": "acme", "name": "Acme Manufacturing"},
            {"name": "CMDB Hub", "accent": "#4cc7b1"},
        )
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 5000)
        text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)
        self.assertIn("Database maintenance", text)
        self.assertIn("DB01", text)
        self.assertIn("APP01", text)
        self.assertIn("Sage 200", text)
        self.assertIn("Financial Controller", text)
        self.assertIn("Rollback plan", text)
        self.assertIn("ConnectWise status", text)

    def test_pdf_embeds_msp_logo_and_document_labels(self):
        change = self._change()
        branding = {
            "name": "IPT CMDB",
            "accent": "#4ed477",
            "logoDataUrl": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
            "reportFooter": "IPT Holdings | Controlled document",
            "confidentialityLabel": "Customer confidential",
        }
        reader = PdfReader(
            BytesIO(
                render_change_pdf(change, {"id": "acme", "name": "Acme Manufacturing"}, branding)
            )
        )
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertGreater(len(list(reader.pages[0].images)), 0)
        self.assertIn("IPT Holdings | Controlled document", text)
        self.assertIn("Customer confidential", text)

    @staticmethod
    def _change():
        return create_change_record(
            {
                "companyId": "acme",
                "scopeAssetIds": ["db"],
                "title": "Database maintenance",
                "changeType": "normal",
                "category": "database",
                "priority": "high",
                "riskLevel": "",
                "outageExpected": True,
                "plannedStart": "2026-07-15T20:00",
                "plannedEnd": "2026-07-15T21:00",
                "reason": "Apply database security updates.",
                "businessImpact": "The customer portal may be unavailable.",
                "implementationPlan": "1. Validate backups.\n2. Apply updates.\n3. Restart services.",
                "validationPlan": "Confirm database health and complete a portal login test.",
                "rollbackPlan": "Restore the database VM snapshot and validate service recovery.",
                "communicationStatus": "required",
                "communicationPlan": "Notify the service owner before and after the window.",
                "assignedTechnician": "tech@example.com",
                "approver": "CAB",
                "notes": "Generated during automated verification.",
            },
            "CHG-2026-0001",
            "change-1",
            {"id": "acme", "name": "Acme Manufacturing"},
            {"id": "admin", "email": "admin@example.com"},
            ASSETS,
            RELATIONSHIPS,
        )

    @staticmethod
    def _virtualization_fixture():
        assets = [
            {
                "id": "cluster",
                "companyId": "acme",
                "name": "PROD-CL01",
                "type": "Virtualization cluster",
                "source": "manual",
                "metadata": {
                    "haEnabled": "yes",
                    "minimumHosts": "1",
                    "capacityStatus": "sufficient",
                    "operationalStatus": "healthy",
                },
            },
            {
                "id": "host-1",
                "companyId": "acme",
                "name": "ESX01",
                "type": "Hypervisor host",
                "source": "manual",
                "metadata": {
                    "powerState": "running",
                    "maintenanceMode": "no",
                    "operationalStatus": "healthy",
                },
            },
            {
                "id": "host-2",
                "companyId": "acme",
                "name": "ESX02",
                "type": "Hypervisor host",
                "source": "manual",
                "metadata": {
                    "powerState": "running",
                    "maintenanceMode": "no",
                    "operationalStatus": "healthy",
                },
            },
            {
                "id": "vm-1",
                "companyId": "acme",
                "name": "ERP01",
                "type": "Virtual machine",
                "source": "manual",
                "metadata": {
                    "haEnabled": "yes",
                    "mobility": "automatic",
                    "protectionStatus": "protected",
                    "virtualizationPlatform": "VMware vSphere",
                    "clusterName": "PROD-CL01",
                },
            },
            {
                "id": "ds-1",
                "companyId": "acme",
                "name": "DATASTORE01",
                "type": "Datastore",
                "source": "manual",
                "metadata": {},
            },
            {
                "id": "erp",
                "companyId": "acme",
                "name": "ERP",
                "type": "Business system",
                "source": "manual",
                "metadata": {"businessOwner": "Finance"},
            },
        ]
        relationships = [
            {
                "fromId": "host-1",
                "toId": "cluster",
                "type": "member_of",
                "impactPolicy": "informational",
            },
            {
                "fromId": "host-2",
                "toId": "cluster",
                "type": "member_of",
                "impactPolicy": "informational",
            },
            {
                "fromId": "vm-1",
                "toId": "cluster",
                "type": "member_of",
                "impactPolicy": "informational",
            },
            {
                "fromId": "host-1",
                "toId": "vm-1",
                "type": "hosts",
                "impactPolicy": "required",
            },
            {
                "fromId": "vm-1",
                "toId": "ds-1",
                "type": "stored_on",
                "impactPolicy": "required",
            },
            {
                "fromId": "erp",
                "toId": "vm-1",
                "type": "depends_on",
                "impactPolicy": "required",
            },
        ]
        return assets, relationships


if __name__ == "__main__":
    unittest.main()
