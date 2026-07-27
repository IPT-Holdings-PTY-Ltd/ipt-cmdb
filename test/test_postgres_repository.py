import os
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
from psycopg import sql

from src.cmdb.migrations import apply_migrations
from src.cmdb.repository import (
    PostgresCmdbRepository,
    change_template_version_uuid,
    hash_password,
)

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL = os.getenv("TEST_POSTGRES_ADMIN_URL", "")
DATABASE_NAME = "cmdb_repository_contract_test"


def database_url(base_url: str, name: str) -> str:
    return urlunparse(urlparse(base_url)._replace(path=f"/{name}"))


@unittest.skipUnless(ADMIN_URL, "TEST_POSTGRES_ADMIN_URL is not configured")
class PostgresRepositoryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATABASE_NAME.startswith("cmdb_repository_contract_test"):
            raise RuntimeError("Unsafe PostgreSQL contract-test database name")
        with (
            psycopg.connect(ADMIN_URL, autocommit=True) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (DATABASE_NAME,),
            )
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DATABASE_NAME))
            )
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE_NAME)))
        cls.target_url = database_url(ADMIN_URL, DATABASE_NAME)

        def connection_factory():
            return psycopg.connect(cls.target_url)

        cls.connection_factory = staticmethod(connection_factory)
        apply_migrations(connection_factory, ROOT)

    @classmethod
    def tearDownClass(cls):
        with (
            psycopg.connect(ADMIN_URL, autocommit=True) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (DATABASE_NAME,),
            )
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DATABASE_NAME))
            )

    def test_canonical_repository_bootstrap_and_governed_crud_round_trip(self):
        state = {
            "companies": [{"id": "acme", "name": "Acme Manufacturing", "externalIds": {}}],
            "users": [
                {
                    "id": "admin",
                    "email": "admin@example.com",
                    "password": "ChangeMe!",
                    "role": "platform_admin",
                    "companyIds": ["*"],
                }
            ],
            "accessGroups": [
                {
                    "id": "all-customers",
                    "name": "All customers",
                    "companyIds": ["*"],
                    "system": True,
                }
            ],
            "contacts": [
                {
                    "id": "finance-owner",
                    "companyId": "acme",
                    "displayName": "Finance Owner",
                    "email": "finance@acme.example",
                    "status": "active",
                    "source": "manual",
                    "syncStatus": "not_synced",
                    "attributes": {},
                }
            ],
            "contactResponsibilities": [
                {
                    "id": "responsibility-1",
                    "companyId": "acme",
                    "assetId": "sage",
                    "contactId": "finance-owner",
                    "role": "business_owner",
                    "isPrimary": True,
                    "escalationOrder": 1,
                    "source": "manual",
                }
            ],
            "integrations": [
                {
                    "id": "connectwise",
                    "name": "ConnectWise Manage",
                    "type": "connectwise",
                    "enabled": False,
                }
            ],
            "syncRuns": [
                {
                    "id": "sync-1",
                    "type": "connectwise",
                    "status": "success",
                    "startedAt": "2026-07-17T08:00:00Z",
                    "finishedAt": "2026-07-17T08:01:00Z",
                    "discovered": 10,
                    "imported": 2,
                    "message": "Contract seed",
                }
            ],
            "assets": [
                {
                    "id": "sage",
                    "companyId": "acme",
                    "name": "Sage 200",
                    "type": "Business system",
                    "status": "Active",
                    "source": "manual",
                    "metadata": {
                        "displayLayer": "business",
                        "lifecycle": "in_service",
                        "operationalStatus": "healthy",
                    },
                },
                {
                    "id": "sql01",
                    "companyId": "acme",
                    "name": "SQL01",
                    "type": "Database",
                    "status": "Active",
                    "source": "manual",
                    "metadata": {
                        "displayLayer": "data",
                        "lifecycle": "in_service",
                        "operationalStatus": "healthy",
                    },
                },
            ],
            "relationships": [
                {
                    "id": "relationship-1",
                    "fromId": "sage",
                    "toId": "sql01",
                    "type": "depends_on",
                    "impactPolicy": "required",
                }
            ],
            "changes": [
                {
                    "id": "change-1",
                    "number": "CHG-2026-0001",
                    "companyId": "acme",
                    "title": "Patch SQL",
                    "status": "draft",
                    "implementationPlan": "Patch SQL01",
                    "validationPlan": "Validate Sage 200",
                    "rollbackPlan": "Restore snapshot",
                    "scopeAssetIds": ["sql01"],
                    "impactSnapshot": [
                        {"assetId": "sql01", "role": "Scope", "depth": 0},
                        {
                            "assetId": "sage",
                            "role": "Downstream impact",
                            "depth": 1,
                            "relationshipPath": ["depends_on"],
                            "pathAssetIds": ["sql01", "sage"],
                        },
                    ],
                    "createdBy": {"email": "admin@example.com"},
                    "revision": 1,
                }
            ],
            "branding": {"acme": {"name": "Acme CMDB", "logoText": "AC", "accent": "#123456"}},
            "mspBranding": {"name": "IPT CMDB", "logoText": "IPT"},
        }
        saved = []
        repository = PostgresCmdbRepository(state, saved.append, self.connection_factory)

        counts = repository.bootstrap()
        self.assertEqual(counts["companies"], 1)
        self.assertEqual(counts["assets"], 2)
        self.assertEqual(counts["relationships"], 1)
        self.assertEqual(counts["changes"], 1)
        self.assertTrue(repository.is_initialized())
        authenticated = repository.authenticate("admin@example.com", "ChangeMe!")
        self.assertEqual(authenticated["role"], "platform_admin")
        actor_id = authenticated["id"]

        standards = repository.list_change_templates("acme")
        self.assertGreaterEqual(len(standards), 6)
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT version.id, version.template_id, version.version
                FROM change_template_versions version
                JOIN change_templates template ON template.id = version.template_id
                WHERE template.system = true
                """
            )
            for version_id, template_id, version in cursor.fetchall():
                self.assertEqual(
                    str(version_id),
                    change_template_version_uuid(str(template_id), int(version)),
                )
        customer_template = repository.create_change_template(
            {
                "companyId": "acme",
                "key": "acme_contract_maintenance",
                "name": "Acme contract maintenance",
                "description": "PostgreSQL contract template",
                "tags": ["contract", "maintenance"],
                "status": "draft",
                "ownerUserId": actor_id,
                "reviewDueDate": "2027-07-24",
                "content": standards[0]["content"],
            },
            actor_id,
        )
        self.assertEqual(customer_template["version"], 1)
        customer_template = repository.update_change_template(
            customer_template["id"],
            {
                "name": customer_template["name"],
                "description": customer_template["description"],
                "tags": customer_template["tags"],
                "status": "published",
                "ownerUserId": actor_id,
                "reviewDueDate": customer_template["reviewDueDate"],
                "content": customer_template["content"],
            },
            1,
            actor_id,
        )
        self.assertEqual(customer_template["version"], 2)
        self.assertEqual(
            repository.get_change_template(customer_template["id"], 1)["status"],
            "published",
        )

        connection = repository.get_integration_connection("connectwise")
        self.assertIsNotNone(connection)
        configured = repository.update_integration_connection(
            "connectwise",
            {
                "configuration": {
                    "baseUrl": "https://api.example.com/v4_6_release/apis/3.0",
                    "companyId": "ipt",
                    "clientId": "client-id",
                    "pageSize": 100,
                },
                "credentialsEncrypted": "ciphertext",
                "credentialsNonce": "nonce",
                "enabled": True,
                "connectionStatus": "configured",
                "expectedRevision": connection["revision"],
            },
            actor_id,
        )
        self.assertEqual(configured["connectionStatus"], "configured")
        impact = repository.integration_lifecycle_impact("connectwise")
        self.assertEqual(impact["syncRuns"], 1)
        paused = repository.change_integration_lifecycle(
            "connectwise",
            "paused",
            "Database maintenance",
            actor_id,
            configured["revision"],
        )
        self.assertFalse(paused["enabled"])
        self.assertEqual(paused["lifecycleStatus"], "paused")
        resumed = repository.change_integration_lifecycle(
            "connectwise",
            "active",
            "Database maintenance completed",
            actor_id,
            paused["revision"],
        )
        self.assertTrue(resumed["enabled"])
        repository.record_company_discovery(
            "connectwise",
            {
                "id": "connectwise-discovery-contract",
                "type": "connectwise",
                "status": "review_required",
                "startedAt": "2026-07-21T10:00:00Z",
                "finishedAt": "2026-07-21T10:00:01Z",
                "discovered": 1,
                "imported": 0,
                "review": 1,
                "message": "One company requires review",
                "attributes": {
                    "operation": "company_discovery",
                    "companyId": "acme",
                },
            },
            [
                {
                    "externalId": "42",
                    "identifier": "ACME",
                    "name": "Acme Manufacturing",
                    "status": "Active",
                    "type": "Customer",
                    "site": "Head office",
                    "deleted": False,
                    "lastUpdated": "2026-07-21T09:00:00Z",
                }
            ],
            actor_id,
        )
        observed = repository.list_provider_companies("connectwise")[0]
        self.assertIsNone(observed["mappedCompanyId"])
        mapped = repository.map_provider_company("connectwise", "42", "acme", actor_id)
        self.assertEqual(mapped["mappedCompanyId"], "acme")
        ci_policy = repository.update_ci_sync_policy(
            "connectwise",
            "acme",
            "42",
            {"syncMode": "continuous_preview", "enabled": True},
            expected_revision=0,
            actor_id=actor_id,
        )
        claimed_policy = repository.claim_due_ci_sync_policy(
            "connectwise",
            "contract-worker-a",
            lease_seconds=120,
        )
        self.assertEqual(claimed_policy["id"], ci_policy["id"])
        self.assertIsNone(
            repository.claim_ci_sync_policy_now(
                ci_policy["id"],
                "contract-worker-b",
                lease_seconds=120,
            )
        )
        first_failure = repository.complete_ci_sync_policy_run(
            ci_policy["id"],
            success=False,
            error="Provider unavailable",
        )
        self.assertTrue(first_failure["backoffActive"])
        self.assertEqual(first_failure["retryDelayMinutes"], 15)
        self.assertIsNotNone(
            repository.claim_ci_sync_policy_now(
                ci_policy["id"],
                "contract-worker-b",
                lease_seconds=120,
            )
        )
        second_failure = repository.complete_ci_sync_policy_run(
            ci_policy["id"],
            success=False,
            error="Provider still unavailable",
        )
        self.assertEqual(second_failure["consecutiveFailures"], 2)
        self.assertEqual(second_failure["retryDelayMinutes"], 30)
        self.assertIsNotNone(
            repository.claim_ci_sync_policy_now(
                ci_policy["id"],
                "contract-worker-c",
                lease_seconds=120,
            )
        )
        recovered_policy = repository.complete_ci_sync_policy_run(
            ci_policy["id"],
            success=True,
        )
        self.assertFalse(recovered_policy["backoffActive"])
        self.assertEqual(recovered_policy["retryDelayMinutes"], 0)
        repository.replace_ci_review_items(
            ci_policy["id"],
            "acme",
            repository.list_sync_runs()[0]["id"],
            [
                {
                    "externalId": "501",
                    "name": "ACME-UNMANAGED-01",
                    "action": "create",
                    "reason": "No canonical identity was found",
                    "record": {
                        "externalId": "501",
                        "name": "ACME-UNMANAGED-01",
                        "type": "Laptop",
                        "status": "Active",
                    },
                }
            ],
            actor_id,
        )
        review_item = repository.list_ci_review_items("connectwise", "acme")[0]
        suppression = repository.ignore_ci_review_items(
            [review_item["id"]], "Outside managed scope", actor_id
        )[0]
        self.assertEqual(
            repository.get_ci_sync_policy("connectwise", "acme", "42")["excludedExternalIds"],
            ["501"],
        )
        self.assertEqual(
            repository.query_integration_object_suppressions(company_id="acme")["total"],
            1,
        )
        self.assertFalse(
            repository.restore_integration_object_suppression(
                suppression["id"], "Now managed", actor_id
            )["active"]
        )
        self.assertTrue(repository.unmap_provider_company("connectwise", "42", actor_id))

        assets = repository.list_assets()
        sage = next(item for item in assets if item["name"] == "Sage 200")
        sql01 = next(item for item in assets if item["name"] == "SQL01")
        self.assertEqual(sage["responsibilities"][0]["contactName"], "Finance Owner")
        self.assertEqual(repository.list_contacts("acme")[0]["responsibilityCount"], 1)
        self.assertEqual(repository.list_relationships()[0]["fromId"], sage["id"])
        self.assertEqual(
            repository.get_change(repository.list_changes()[0]["id"])["number"], "CHG-2026-0001"
        )
        self.assertEqual(
            [item["number"] for item in repository.list_changes(company_id="acme")],
            ["CHG-2026-0001"],
        )
        self.assertEqual(
            [item["number"] for item in repository.list_changes(asset_id=sql01["id"])],
            ["CHG-2026-0001"],
        )
        self.assertEqual(repository.list_changes(asset_id=str(uuid.uuid4())), [])
        change_id = repository.list_changes()[0]["id"]
        assigned_change = repository.get_change(change_id)
        assigned_change.update(
            {
                "assignedUserId": actor_id,
                "assignedTechnician": "Admin",
                "assignmentHistory": [
                    {
                        "id": str(uuid.uuid4()),
                        "previousUserId": None,
                        "previousDisplayName": "",
                        "assignedUserId": actor_id,
                        "assignedDisplayName": "Admin",
                        "reason": "Assign for PostgreSQL contract verification",
                        "actorId": actor_id,
                        "actorEmail": "admin@example.com",
                        "createdAt": "2026-07-24T12:00:00Z",
                    }
                ],
                "templateId": customer_template["id"],
                "templateVersion": customer_template["version"],
                "templateSnapshot": {
                    "id": customer_template["id"],
                    "key": customer_template["key"],
                    "name": customer_template["name"],
                    "companyId": "acme",
                    "version": customer_template["version"],
                    "content": customer_template["content"],
                },
                "templateParameters": {"contract_reference": "PSQL-1"},
                "revision": 2,
            }
        )
        persisted_assignment = repository.update_change(
            change_id,
            assigned_change,
            actor_id,
            action="reassigned",
            reason="Assign for PostgreSQL contract verification",
        )
        self.assertEqual(persisted_assignment["assignedUserId"], actor_id)
        self.assertEqual(
            persisted_assignment["assignmentHistory"][0]["assignedDisplayName"], "Admin"
        )
        self.assertEqual(
            persisted_assignment["templateSnapshot"]["name"],
            "Acme contract maintenance",
        )
        approval = repository.create_change_approval_request(
            {
                "id": str(uuid.uuid4()),
                "changeId": change_id,
                "companyId": "acme",
                "batchId": str(uuid.uuid4()),
                "changeRevision": 1,
                "approverContactId": sage["responsibilities"][0]["contactId"],
                "approverName": "Finance Owner",
                "approverEmail": "finance@acme.example",
                "responsibilityRole": "business_owner",
                "scope": [{"type": "business_system", "id": sage["id"], "name": "Sage 200"}],
                "status": "pending",
                "tokenHash": "a" * 64,
                "expiresAt": "2099-01-01T00:00:00Z",
            },
            actor_id,
        )
        self.assertNotIn("tokenHash", approval)
        self.assertEqual(
            repository.list_change_approval_requests(change_id)[0]["status"], "pending"
        )
        self.assertEqual(
            repository.get_change_approval_request_by_token("a" * 64)["approverEmail"],
            "finance@acme.example",
        )
        decided = repository.decide_change_approval_request("a" * 64, "approved", "Approved")
        self.assertEqual(decided["status"], "approved")
        replacement = repository.create_change_approval_request(
            {
                **approval,
                "id": str(uuid.uuid4()),
                "batchId": str(uuid.uuid4()),
                "status": "pending",
                "tokenHash": "b" * 64,
                "expiresAt": "2099-01-01T00:00:00Z",
            },
            actor_id,
        )
        self.assertEqual(repository.revoke_change_approval_requests(change_id, actor_id), 1)
        self.assertEqual(
            next(
                item
                for item in repository.list_change_approval_requests(change_id)
                if item["id"] == replacement["id"]
            )["status"],
            "revoked",
        )
        revocation_events = repository.list_audit_events(
            "acme",
            action="revoked",
            entity_type="change_approval_request",
            entity_id=replacement["id"],
        )
        self.assertEqual(len(revocation_events), 1)
        self.assertEqual(revocation_events[0]["after"]["status"], "revoked")
        self.assertEqual(revocation_events[0]["actorUserId"], actor_id)
        self.assertEqual(repository.next_change_number(2026), "CHG-2026-0002")
        self.assertEqual(repository.get_msp_branding()["name"], "IPT CMDB")
        self.assertEqual(repository.get_company_branding("acme")["name"], "Acme CMDB")
        sync_statuses = {item["status"] for item in repository.list_sync_runs()}
        self.assertIn("success", sync_statuses)
        self.assertIn("review_required", sync_statuses)
        filtered_sync_runs = repository.list_sync_runs(
            "connectwise",
            "review_required",
            "company_discovery",
            "acme",
            10,
        )
        self.assertEqual(len(filtered_sync_runs), 1)
        self.assertEqual(
            filtered_sync_runs[0]["attributes"]["operation"],
            "company_discovery",
        )
        self.assertEqual(
            repository.list_sync_runs(
                "connectwise",
                "review_required",
                "company_discovery",
                company_ids={"northwind"},
            ),
            [],
        )
        self.assertEqual(repository.list_notification_preferences(), [])
        self.assertEqual(repository.list_notification_preferences("acme"), [])
        self.assertEqual(repository.list_notification_events(), [])
        self.assertEqual(repository.list_notification_events("acme"), [])
        portable_templates = repository.export_state()["changeTemplates"]
        exported_customer_template = next(
            item for item in portable_templates if item["id"] == customer_template["id"]
        )
        self.assertEqual(
            [item["version"] for item in exported_customer_template["versions"]],
            [1, 2],
        )

        updated = repository.update_asset(
            sql01["id"], {"name": "SQL-PROD", "metadata": sql01["metadata"]}, actor_id
        )
        self.assertEqual(updated["name"], "SQL-PROD")
        exception = repository.create_data_quality_exception(
            {
                "id": "owner-gap:sql01",
                "companyId": "acme",
                "ruleKey": "owner-gap",
                "entityId": sql01["id"],
                "reason": "Temporary exception",
            },
            actor_id,
        )
        self.assertEqual(exception["state"], "active")
        self.assertEqual(
            repository.resolve_data_quality_exception(exception["id"], actor_id)["state"],
            "resolved",
        )

        rule = {
            "companyId": "acme",
            "ciType": "Database",
            "fieldName": "name",
            "provider": "ncentral",
            "priority": 10,
        }
        repository.upsert_field_authority(rule, actor_id)
        self.assertEqual(repository.list_field_authority("acme"), [rule])
        self.assertTrue(
            repository.delete_field_authority("acme", "Database", "name", "ncentral", actor_id)
        )
        audit = repository.list_audit_events(
            "acme", action="updated", entity_type="configuration_item", search="SQL-PROD"
        )
        self.assertEqual(audit[0]["actorLabel"], "admin@example.com")
        self.assertGreaterEqual(len(repository.list_audit_events("acme")), 3)
        exported = repository.export_state()
        self.assertEqual(len(exported["companies"]), 1)
        self.assertEqual(exported["auditEvents"], [])

        managed_group = repository.create_access_group(
            {
                "id": "managed-infrastructure",
                "name": "Managed infrastructure",
                "description": "Reusable managed-services customer scope.",
                "companyIds": ["acme"],
                "ownerUserId": actor_id,
                "membershipMode": "manual",
                "membershipRules": {},
                "system": False,
            },
            actor_id,
        )
        self.assertEqual(managed_group["revision"], 1)
        self.assertEqual(managed_group["ownerLabel"], "admin")
        updated_group = repository.update_access_group(
            managed_group["id"],
            {
                "description": "Reviewed managed-services customer scope.",
                "expectedRevision": 1,
            },
            actor_id,
        )
        self.assertEqual(updated_group["revision"], 2)
        self.assertIsNone(
            repository.update_access_group(
                managed_group["id"],
                {"description": "Stale overwrite", "expectedRevision": 1},
                actor_id,
            )
        )

        operator = repository.create_user(
            {
                "id": "group-only-operator",
                "email": "operator@example.com",
                "role": "msp_operator",
                "companyIds": [],
                "groupIds": ["all-customers"],
                "accountType": "root",
            },
            "Temporary!42",
            actor_id,
        )
        self.assertEqual(operator["role"], "msp_operator")
        self.assertEqual(operator["companyIds"], ["acme"])
        repository.create_company(
            {"id": "contoso", "name": "Contoso Services", "externalIds": {}}, actor_id
        )
        refreshed_operator = repository.authenticate("operator@example.com", "Temporary!42")
        self.assertEqual(refreshed_operator["role"], "msp_operator")
        self.assertEqual(refreshed_operator["companyIds"], ["acme", "contoso"])

        api_user = repository.update_user(
            operator["id"],
            {
                "email": "operator@example.com",
                "displayName": "Automation Operator",
                "role": "msp_operator",
                "accountType": "root",
                "directCompanyIds": [],
                "groupIds": ["all-customers"],
                "apiAccessEnabled": True,
                "mfaRequired": True,
            },
            actor_id,
            reason="Enable governed automation",
        )
        self.assertTrue(api_user["apiAccessEnabled"])
        self.assertTrue(api_user["mfaRequired"])
        repository.save_mfa_enrollment(operator["id"], "encrypted-seed", "nonce", actor_id)
        self.assertEqual(repository.get_mfa_credential(operator["id"])["status"], "pending")
        self.assertTrue(
            repository.enable_mfa(
                operator["id"], 100, [hash_password("AAAA-BBBB-CCCC-DDDD")], actor_id
            )
        )
        self.assertTrue(repository.accept_mfa_counter(operator["id"], 101))
        self.assertFalse(repository.accept_mfa_counter(operator["id"], 101))
        self.assertTrue(repository.consume_recovery_code(operator["id"], "AAAA-BBBB-CCCC-DDDD"))
        self.assertFalse(repository.consume_recovery_code(operator["id"], "AAAA-BBBB-CCCC-DDDD"))
        challenge_hash = "b" * 64
        repository.create_login_challenge(
            {
                "tokenHash": challenge_hash,
                "userId": operator["id"],
                "purpose": "verify",
                "maxAttempts": 5,
                "expiresAt": "2099-01-01T00:00:00Z",
            }
        )
        self.assertEqual(repository.get_login_challenge(challenge_hash)["purpose"], "verify")
        self.assertEqual(repository.record_login_challenge_attempt(challenge_hash), 1)
        repository.consume_login_challenge(challenge_hash)
        self.assertIsNone(repository.get_login_challenge(challenge_hash))
        session_hash = "c" * 64
        repository.create_session(session_hash, operator["id"], "2099-01-01T00:00:00Z")
        self.assertEqual(repository.authenticate_session(session_hash), operator["id"])
        self.assertTrue(repository.revoke_session(session_hash))
        self.assertIsNone(repository.authenticate_session(session_hash))
        token_hash = "a" * 64
        api_token = repository.create_api_token(
            {
                "id": str(uuid.uuid4()),
                "userId": operator["id"],
                "name": "Repository contract",
                "tokenPrefix": "cmdb_pat_contract",
                "tokenHash": token_hash,
                "scopes": ["cmdb:read"],
                "companyIds": ["acme"],
                "expiresAt": "2099-01-01T00:00:00Z",
            },
            actor_id,
        )
        self.assertNotIn("tokenHash", api_token)
        authenticated_token = repository.authenticate_api_token(token_hash)
        self.assertEqual(authenticated_token["user"]["id"], operator["id"])
        self.assertEqual(authenticated_token["token"]["companyIds"], ["acme"])
        revoked_token = repository.revoke_api_token(api_token["id"], actor_id)
        self.assertIsNotNone(revoked_token["revokedAt"])
        self.assertIsNone(repository.authenticate_api_token(token_hash))
        self.assertTrue(repository.delete_access_group(managed_group["id"], actor_id))
        self.assertTrue(saved)


if __name__ == "__main__":
    unittest.main()
