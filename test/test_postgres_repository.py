import os
import unittest
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
from psycopg import sql

from src.cmdb.migrations import apply_migrations
from src.cmdb.repository import PostgresCmdbRepository

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

        assets = repository.list_assets()
        sage = next(item for item in assets if item["name"] == "Sage 200")
        sql01 = next(item for item in assets if item["name"] == "SQL01")
        self.assertEqual(sage["responsibilities"][0]["contactName"], "Finance Owner")
        self.assertEqual(repository.list_contacts("acme")[0]["responsibilityCount"], 1)
        self.assertEqual(repository.list_relationships()[0]["fromId"], sage["id"])
        self.assertEqual(
            repository.get_change(repository.list_changes()[0]["id"])["number"], "CHG-2026-0001"
        )
        self.assertEqual(repository.next_change_number(2026), "CHG-2026-0002")
        self.assertEqual(repository.get_msp_branding()["name"], "IPT CMDB")
        self.assertEqual(repository.get_company_branding("acme")["name"], "Acme CMDB")
        self.assertEqual(repository.list_sync_runs()[0]["status"], "success")

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
        self.assertTrue(saved)


if __name__ == "__main__":
    unittest.main()
