import base64
import json
import os
import unittest
import uuid
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


class DatabaseConfigurationSecurityTests(unittest.TestCase):
    def setUp(self):
        self.key_bytes = bytes(range(32))
        self.encoded_key = base64.urlsafe_b64encode(self.key_bytes).decode("ascii")
        self.database_url = "postgresql://unit-user:" + "credential-value@db/cmdb"
        self.temp_root = Path(__file__).resolve().parents[1] / "tmp"
        self.temp_root.mkdir(exist_ok=True)
        self.test_paths: list[Path] = []

    def tearDown(self):
        for path in self.test_paths:
            path.unlink(missing_ok=True)
            path.with_suffix(f"{path.suffix}.tmp").unlink(missing_ok=True)

    def temporary_path(self, name: str) -> Path:
        path = self.temp_root / f"security-{uuid.uuid4()}-{name}"
        self.test_paths.append(path)
        return path

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

        self.assertEqual(response, {"status": "alive", "api": "FastAPI"})

    def test_readiness_requires_canonical_postgres_repository(self):
        with (
            patch.object(core, "DATABASE_MODE", "PostgreSQL unavailable"),
            patch.object(backend_main.REPOSITORY, "mode", "local"),
        ):
            response = backend_main.readiness()

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("PostgreSQL unavailable", response.body.decode())

    def test_readiness_accepts_canonical_postgres_repository(self):
        with (
            patch.object(core, "DATABASE_MODE", "PostgreSQL"),
            patch.object(backend_main.REPOSITORY, "mode", "canonical_postgresql"),
        ):
            response = backend_main.readiness()

        self.assertEqual(response.status_code, 200)

    def test_health_does_not_expose_repository_exception_text(self):
        with (
            patch.object(core, "DATABASE_MODE", "PostgreSQL unavailable"),
            patch.object(core, "DATABASE_ERROR", "server=db.internal user=cmdb password=hidden"),
        ):
            response = backend_main.health()

        self.assertEqual(response["status"], "degraded")
        self.assertFalse(response["databaseAvailable"])
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
        root = temporary_base / f"static-files-{uuid.uuid4()}"
        frontend_dist = root / "dist"
        index_path = frontend_dist / "index.html"
        source_path = root / "app.py"
        frontend_dist.mkdir(parents=True)
        try:
            index_path.write_text("<h1>CMDB Hub</h1>", encoding="utf-8")
            source_path.write_text("CMDB Hub transitional state", encoding="utf-8")
            isolated_api = FastAPI()
            backend_main.mount_frontend(isolated_api, frontend_dist)

            response = TestClient(isolated_api).get("/%2e%2e/app.py")
        finally:
            index_path.unlink(missing_ok=True)
            source_path.unlink(missing_ok=True)
            frontend_dist.rmdir()
            root.rmdir()

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("CMDB Hub transitional state", response.text)
