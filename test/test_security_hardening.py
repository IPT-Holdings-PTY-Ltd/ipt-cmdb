import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app as core
import backend.main as backend_main
from src.cmdb.database_config import (
    DatabaseConfigError,
    decrypt_database_url,
    encryption_key,
    write_encrypted_database_url,
)
from src.cmdb.version import APPLICATION_VERSION


class DatabaseConfigurationSecurityTests(unittest.TestCase):
    def setUp(self):
        self.key_bytes = bytes(range(32))
        self.encoded_key = base64.urlsafe_b64encode(self.key_bytes).decode("ascii")
        self.database_url = "postgresql://unit-user:" + "credential-value@db/cmdb"
        temporary_base = Path(__file__).resolve().parents[1] / "tmp"
        temporary_base.mkdir(exist_ok=True)
        self.temp_directory = tempfile.TemporaryDirectory(
            prefix="cmdb-security-",
            dir=temporary_base,
        )
        self.addCleanup(self.temp_directory.cleanup)
        self.temp_root = Path(self.temp_directory.name)

    def temporary_path(self, name: str) -> Path:
        return self.temp_root / name

    def test_encrypted_configuration_round_trip_contains_no_plaintext_credentials(self):
        path = self.temporary_path("database-config.json")
        write_encrypted_database_url(path, self.database_url, self.key_bytes)

        persisted = path.read_text(encoding="utf-8")
        document = json.loads(persisted)

        self.assertNotIn(self.database_url, persisted)
        self.assertNotIn("credential-value", persisted)
        self.assertEqual(document["algorithm"], "AES-256-GCM")
        self.assertEqual(decrypt_database_url(document, self.key_bytes), self.database_url)

    def test_key_can_be_loaded_from_a_container_secret_file(self):
        key_path = self.temporary_path("database-config-key")
        key_path.write_text(self.encoded_key, encoding="utf-8")

        self.assertEqual(encryption_key(value="", key_file=str(key_path)), self.key_bytes)

    def test_legacy_plaintext_configuration_migrates_when_key_is_available(self):
        path = self.temporary_path("database-config.json")
        path.write_text(json.dumps({"url": self.database_url}), encoding="utf-8")
        environment = {"DATABASE_CONFIG_ENCRYPTION_KEY": self.encoded_key}

        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(core, "DATABASE_CONFIG_FILE", path),
            self.assertLogs("cmdb.core", level="WARNING"),
        ):
            database_url, source, error = core.saved_database_url()

        self.assertEqual(database_url, self.database_url)
        self.assertEqual(source, "encrypted local configuration")
        self.assertIsNone(error)
        self.assertNotIn("url", json.loads(path.read_text(encoding="utf-8")))

    def test_legacy_plaintext_configuration_is_blocked_without_a_key(self):
        path = self.temporary_path("database-config.json")
        path.write_text(json.dumps({"url": self.database_url}), encoding="utf-8")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(core, "DATABASE_CONFIG_FILE", path),
            self.assertLogs("cmdb.core", level="ERROR"),
        ):
            database_url, source, error = core.saved_database_url()

        self.assertIsNone(database_url)
        self.assertEqual(source, "encrypted local configuration")
        self.assertIn("configuration key", error or "")

    def test_invalid_key_is_rejected_without_echoing_key_material(self):
        invalid_key = "not-a-valid-key"
        with self.assertRaises(DatabaseConfigError) as context:
            encryption_key(invalid_key)
        self.assertNotIn(invalid_key, str(context.exception))


class ApiExposureSecurityTests(unittest.TestCase):
    def test_liveness_is_independent_of_database_state(self):
        with patch.object(core, "DATABASE_MODE", "PostgreSQL unavailable"):
            response = backend_main.liveness()

        self.assertEqual(response["status"], "alive")
        self.assertEqual(response["api"], "FastAPI")
        self.assertEqual(response["applicationVersion"], APPLICATION_VERSION)

    def test_readiness_requires_canonical_postgres_repository(self):
        with (
            patch.object(core, "DATABASE_MODE", "PostgreSQL unavailable"),
            patch.object(backend_main.REPOSITORY, "mode", "local"),
            patch.object(core, "database_readiness") as probe,
        ):
            response = backend_main.readiness()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            json.loads(response.body)["applicationVersion"],
            APPLICATION_VERSION,
        )
        self.assertNotIn("PostgreSQL unavailable", response.body.decode())
        probe.assert_not_called()

    def test_readiness_accepts_canonical_postgres_repository(self):
        with (
            patch.object(core, "DATABASE_MODE", "PostgreSQL"),
            patch.object(backend_main.REPOSITORY, "mode", "canonical_postgresql"),
            patch.object(
                core,
                "database_readiness",
                return_value={
                    "databaseAvailable": True,
                    "schemaCurrent": True,
                    "schemaVersion": core.SCHEMA_VERSION,
                    "expectedSchemaVersion": core.SCHEMA_VERSION,
                },
            ),
        ):
            response = backend_main.readiness()

        self.assertEqual(response.status_code, 200)
        readiness_body = json.loads(response.body)
        self.assertTrue(readiness_body["schemaCurrent"])
        self.assertEqual(readiness_body["applicationVersion"], APPLICATION_VERSION)
        self.assertEqual(
            readiness_body["schemaHistorySha256"],
            core.SCHEMA_HISTORY_SHA256,
        )

    def test_readiness_fails_closed_on_live_database_error_without_leaking_details(self):
        secret = "postgresql://cmdb:password-value@db.internal/cmdb"
        with (
            patch.object(core, "DATABASE_MODE", "PostgreSQL"),
            patch.object(backend_main.REPOSITORY, "mode", "canonical_postgresql"),
            patch.object(core, "database_readiness", side_effect=RuntimeError(secret)),
        ):
            response = backend_main.readiness()

        self.assertEqual(response.status_code, 503)
        self.assertFalse(json.loads(response.body)["databaseAvailable"])
        self.assertNotIn(secret, response.body.decode())
        self.assertNotIn("db.internal", response.body.decode())

    def test_readiness_rejects_schema_drift_while_database_is_reachable(self):
        with (
            patch.object(core, "DATABASE_MODE", "PostgreSQL"),
            patch.object(backend_main.REPOSITORY, "mode", "canonical_postgresql"),
            patch.object(
                core,
                "database_readiness",
                return_value={
                    "databaseAvailable": True,
                    "schemaCurrent": False,
                    "schemaVersion": "older",
                    "expectedSchemaVersion": core.SCHEMA_VERSION,
                },
            ),
        ):
            response = backend_main.readiness()

        body = json.loads(response.body)
        self.assertEqual(response.status_code, 503)
        self.assertTrue(body["databaseAvailable"])
        self.assertFalse(body["schemaCurrent"])

    def test_health_does_not_expose_repository_exception_text(self):
        with (
            patch.object(core, "DATABASE_MODE", "PostgreSQL unavailable"),
            patch.object(core, "DATABASE_ERROR", "server=db.internal user=cmdb password=hidden"),
        ):
            response = backend_main.health()

        self.assertEqual(response["status"], "degraded")
        self.assertFalse(response["databaseAvailable"])
        self.assertEqual(response["applicationVersion"], APPLICATION_VERSION)
        self.assertIn("sourceCommit", response)
        self.assertIn("imageDigest", response)
        self.assertEqual(response["schemaHistorySha256"], core.SCHEMA_HISTORY_SHA256)
        self.assertNotIn("repositoryError", response)
        self.assertNotIn("db.internal", json.dumps(response))

    def test_setup_health_does_not_claim_database_availability(self):
        with patch.object(core, "DATABASE_MODE", "database setup"):
            response = backend_main.health()

        self.assertEqual(response["status"], "setup_required")
        self.assertFalse(response["databaseAvailable"])

    def test_database_connection_failure_returns_a_stable_public_message(self):
        internal_error = "connection to db.internal failed for user cmdb"
        with (
            patch.object(core, "database_diagnostics", side_effect=RuntimeError(internal_error)),
            self.assertLogs("cmdb.core", level="ERROR") as logs,
        ):
            result = core.test_database_url("postgresql://invalid")

        self.assertFalse(result["ok"])
        self.assertNotIn("db.internal", result["error"])
        self.assertEqual(
            result["error"],
            "Unable to connect using the supplied database settings",
        )
        self.assertIn("db.internal", " ".join(logs.output))

    def test_static_file_mount_rejects_encoded_parent_traversal(self):
        temporary_base = Path(__file__).resolve().parents[1] / "tmp"
        temporary_base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="cmdb-static-files-",
            dir=temporary_base,
        ) as temp_dir:
            root = Path(temp_dir)
            frontend_dist = root / "dist"
            index_path = frontend_dist / "index.html"
            source_path = root / "app.py"
            frontend_dist.mkdir()
            index_path.write_text("<h1>CMDB Hub</h1>", encoding="utf-8")
            source_path.write_text("CMDB Hub transitional state", encoding="utf-8")
            isolated_api = FastAPI()
            backend_main.mount_frontend(isolated_api, frontend_dist)

            with TestClient(isolated_api) as client:
                response = client.get("/%2e%2e/app.py")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("CMDB Hub transitional state", response.text)

    def test_login_identifier_hash_is_normalized_stable_and_opaque(self):
        first = backend_main._login_identifier_hash(" ADMIN@Example.com ")
        second = backend_main._login_identifier_hash("admin@example.com")

        self.assertEqual(first, second)
        self.assertNotIn("admin@example.com", first)
        self.assertEqual(len(first), 64)
