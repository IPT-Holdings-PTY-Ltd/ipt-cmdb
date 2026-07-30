"""Contract and check-only tests for guarded container updates."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
import unittest
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
POWERSHELL = shutil.which("pwsh")
POSIX_SHELL = shutil.which("sh")
NATIVE_POSIX_TESTS = os.name == "posix" and POSIX_SHELL is not None
OLD_DIGEST = "sha256:" + ("1" * 64)
TARGET_DIGEST = "sha256:" + ("2" * 64)
IMAGE_NAME = "ghcr.io/ipt-holdings-pty-ltd/ipt-cmdb"


def write_release_fixture(
    directory: Path,
    *,
    database_change: str = "none",
    rollback_policy: str = "previous-image-compatible",
) -> Path:
    """Write one checksummed release manifest suitable for updater validation."""

    manifest = {
        "manifestVersion": 1,
        "version": "0.4.0",
        "tag": "v0.4.0",
        "commit": "a" * 40,
        "prerelease": False,
        "sourceTimestamp": "2026-07-30T08:00:00Z",
        "image": {
            "name": IMAGE_NAME,
            "digest": TARGET_DIGEST,
            "reference": f"{IMAGE_NAME}@{TARGET_DIGEST}",
            "versionTag": f"{IMAGE_NAME}:0.4.0",
            "commitTag": f"{IMAGE_NAME}:sha-{'a' * 40}",
            "architectures": ["linux/amd64", "linux/arm64"],
        },
        "database": {
            "schemaVersion": "2026.07.29.2",
            "schemaHistorySha256": "b" * 64,
            "schemaHistoryAlgorithm": "test fixture",
            "postgresqlMajor": 16,
            "changeClassification": database_change,
            "backupRequired": database_change != "none",
            "rollbackPolicy": rollback_policy,
            "rollbackBoundary": "schema-version-change-requires-database-restore",
            "migrationPolicy": "forward-only",
        },
    }
    manifest_path = directory / "release-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (directory / "SHA256SUMS").write_text(
        f"{digest}  release-manifest.json\n",
        encoding="utf-8",
    )
    return manifest_path


def write_appliance_fixture(
    directory: Path,
    instance_name: str,
    *,
    port: int = 9,
) -> Path:
    """Create the non-secret deployment metadata required for a check-only run."""

    instance = directory / "instance"
    backup = instance / "backups"
    backup.mkdir(parents=True)
    (instance / ".env.appliance").write_text(
        "\n".join(
            (
                f"COMPOSE_PROJECT_NAME=cmdb-{instance_name}",
                f"CMDB_IMAGE={IMAGE_NAME}@{OLD_DIGEST}",
                f"CMDB_IMAGE_DIGEST={OLD_DIGEST}",
                "CMDB_BIND_ADDRESS=127.0.0.1",
                f"CMDB_PORT={port}",
                f"CMDB_BACKUP_DIRECTORY={backup.as_posix()}",
                f"CMDB_SECRET_DIRECTORY={(instance / 'secrets').as_posix()}",
                "BOOTSTRAP_ADMIN_EMAIL=owner@example.com",
                "PUBLIC_BASE_URL=https://cmdb.example.test",
                "",
            )
        ),
        encoding="utf-8",
    )
    return instance


def write_external_fixture(directory: Path, *, port: int = 9) -> Path:
    """Create non-secret metadata for an external-PostgreSQL deployment."""

    instance = directory / "instance"
    instance.mkdir(parents=True)
    (instance / ".env.production").write_text(
        "\n".join(
            (
                f"CMDB_IMAGE={IMAGE_NAME}@{OLD_DIGEST}",
                f"CMDB_IMAGE_DIGEST={OLD_DIGEST}",
                "CMDB_BIND_ADDRESS=127.0.0.1",
                f"CMDB_PORT={port}",
                "PUBLIC_BASE_URL=https://cmdb.example.test",
                "BOOTSTRAP_ADMIN_EMAIL=owner@example.com",
                "DATABASE_URL_SECRET_FILE=/run/secrets/database-url.txt",
                "MFA_ENCRYPTION_KEY_SECRET_FILE=/run/secrets/mfa-encryption-key.txt",
                "BOOTSTRAP_ADMIN_PASSWORD_SECRET_FILE=/run/secrets/bootstrap-password.txt",
                "",
            )
        ),
        encoding="utf-8",
    )
    return instance


@contextmanager
def runtime_metadata_server() -> Iterator[int]:
    """Serve deterministic current health metadata for PowerShell updater tests."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path not in {"/api/health", "/api/ready"}:
                self.send_error(404)
                return
            payload = {
                "status": "ok" if self.path == "/api/health" else "ready",
                "applicationVersion": "0.3.0" if self.path == "/api/health" else "0.4.0",
                "imageDigest": OLD_DIGEST if self.path == "/api/health" else TARGET_DIGEST,
                "schemaCurrent": True,
                "schemaVersion": "2026.07.29.2",
                "expectedSchemaVersion": "2026.07.29.2",
                "schemaHistorySha256": "b" * 64,
            }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def fake_docker_environment(
    directory: Path,
    *,
    fail_migration: bool = False,
    include_postgres: bool = True,
) -> tuple[dict[str, str], Path]:
    """Put a non-mutating Docker command recorder first on PATH."""

    binary_directory = directory / "bin"
    binary_directory.mkdir()
    log_path = directory / "docker.log"
    if os.name == "nt":
        executable = binary_directory / "docker.cmd"
        failure = (
            'echo %*| findstr /C:"migrate_postgres.py" >nul && exit /b 1\r\n'
            if fail_migration
            else ""
        )
        executable.write_text(
            '@echo off\r\necho %*>>"%FAKE_DOCKER_LOG%"\r\n'
            'echo %*| findstr /C:"compose version --short" >nul && '
            "(echo v2.30.0& exit /b 0)\r\n"
            'echo %*| findstr /C:"com.docker.compose.service=worker" >nul && '
            "exit /b 0\r\n"
            'echo %*| findstr /C:"com.docker.compose.service=cmdb" >nul && '
            "(echo aaaaaaaaaaaa& exit /b 0)\r\n"
            'if "%1"=="inspect" (echo true^|healthy& exit /b 0)\r\n' + failure + "exit /b 0\r\n",
            encoding="utf-8",
        )
    else:
        executable = binary_directory / "docker"
        configured_images = "postgres:16-alpine\\n" if include_postgres else ""
        executable.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$FAKE_DOCKER_LOG"\n'
            'case "$*" in\n'
            '  "compose version --short") printf "v2.30.0\\n" ;;\n'
            f'  *"config --images"*) printf "{configured_images}" ;;\n'
            '  *"/api/health"*) printf '
            "'\"%s\\\\n\"' "
            '\'{"status":"ok","expectedSchemaVersion":"2026.07.29.2"}\' '
            ";;\n"
            '  *"/api/ready"*) printf '
            "'\"%s\\\\n\"' "
            '\'{"status":"ready","schemaCurrent":true,'
            '"applicationVersion":"0.4.0",'
            f'"imageDigest":"{TARGET_DIGEST}",'
            '"schemaVersion":"2026.07.29.2",'
            '"expectedSchemaVersion":"2026.07.29.2",'
            f'"schemaHistorySha256":"{"b" * 64}"}}\' '
            ";;\n"
            "esac\n",
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    environment = dict(os.environ)
    environment["PATH"] = str(binary_directory) + os.pathsep + environment.get("PATH", "")
    environment["FAKE_DOCKER_LOG"] = str(log_path)
    environment.pop("GITHUB_TOKEN", None)
    return environment, log_path


class ApplianceUpdaterContractTests(unittest.TestCase):
    """Keep both updater entry points aligned with the release safety boundary."""

    def test_both_updaters_enforce_the_guarded_update_contract(self) -> None:
        powershell = (SCRIPTS / "Update-Cmdb.ps1").read_text(encoding="utf-8")
        shell = (SCRIPTS / "update-cmdb.sh").read_text(encoding="utf-8")

        for source in (powershell, shell):
            with self.subTest(script="PowerShell" if source is powershell else "POSIX"):
                self.assertIn("schema-version-change-requires-database-restore", source)
                self.assertIn("database-restore-required", source)
                self.assertIn("SHA256SUMS", source)
                self.assertIn("migrate_postgres.py", source)
                self.assertTrue("api/ready" in source or "fetch_runtime_endpoint ready" in source)
                self.assertIn("CMDB_IMAGE_DIGEST", source)
                self.assertIn("backup", source.lower())
                self.assertNotIn("pull postgres", source.lower())
                self.assertNotIn("up -d postgres", source.lower())

    @unittest.skipUnless(POWERSHELL, "PowerShell 7 is not available")
    def test_powershell_check_is_read_only(self) -> None:
        temporary_base = ROOT / "tmp"
        temporary_base.mkdir(exist_ok=True)
        directory = temporary_base / f"cmdb-update-powershell-{uuid.uuid4().hex}"
        directory.mkdir()
        try:
            release = directory / "release"
            release.mkdir()
            manifest = write_release_fixture(release)
            with runtime_metadata_server() as port:
                instance = write_appliance_fixture(directory, "contract", port=port)
                environment, docker_log = fake_docker_environment(directory)
                before = hashlib.sha256((instance / ".env.appliance").read_bytes()).hexdigest()

                result = subprocess.run(
                    [
                        POWERSHELL or "pwsh",
                        "-NoProfile",
                        "-NonInteractive",
                        "-File",
                        str(SCRIPTS / "Update-Cmdb.ps1"),
                        "-Action",
                        "Check",
                        "-InstanceName",
                        "contract",
                        "-InstanceRoot",
                        str(instance),
                        "-ManifestPath",
                        str(manifest),
                    ],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("different manifest image digest is available", result.stdout)
            after = hashlib.sha256((instance / ".env.appliance").read_bytes()).hexdigest()
            self.assertEqual(after, before)
            self.assertFalse((instance / ".cmdb-update.lock").exists())
            docker_calls = docker_log.read_text(encoding="utf-8").lower()
            for forbidden in (" pull ", " stop ", " up ", " run "):
                self.assertNotIn(forbidden, f" {docker_calls} ")
        finally:
            if directory.exists():
                shutil.rmtree(directory)

    @unittest.skipUnless(POWERSHELL, "PowerShell 7 is not available")
    def test_powershell_post_migration_failure_stops_target_web(self) -> None:
        temporary_base = ROOT / "tmp"
        temporary_base.mkdir(exist_ok=True)
        directory = temporary_base / f"cmdb-update-powershell-failure-{uuid.uuid4().hex}"
        directory.mkdir()
        try:
            release = directory / "release"
            release.mkdir()
            manifest = write_release_fixture(release)
            with runtime_metadata_server() as port:
                instance = write_appliance_fixture(directory, "failure", port=port)
                backup = instance / "backups"
                backup_name = f"cmdb-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.dump"
                backup_path = backup / backup_name
                backup_path.write_bytes(b"verified update test backup")
                future_timestamp = time.time() + 60
                os.utime(backup_path, (future_timestamp, future_timestamp))
                backup_digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
                (backup / f"{backup_name}.sha256").write_text(
                    f"{backup_digest}  {backup_name}\n",
                    encoding="utf-8",
                )
                environment, docker_log = fake_docker_environment(
                    directory,
                    fail_migration=True,
                )

                result = subprocess.run(
                    [
                        POWERSHELL or "pwsh",
                        "-NoProfile",
                        "-NonInteractive",
                        "-File",
                        str(SCRIPTS / "Update-Cmdb.ps1"),
                        "-Action",
                        "Apply",
                        "-InstanceName",
                        "failure",
                        "-InstanceRoot",
                        str(instance),
                        "-ManifestPath",
                        str(manifest),
                        "-ApproveVersion",
                        "0.4.0",
                        "-ApproveManifestSha256",
                        hashlib.sha256(manifest.read_bytes()).hexdigest(),
                    ],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )

            self.assertNotEqual(result.returncode, 0)
            docker_calls = docker_log.read_text(encoding="utf-8").lower()
            self.assertGreaterEqual(
                docker_calls.count("stop cmdb"),
                2,
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}\ndocker:\n{docker_calls}",
            )
            environment_text = (instance / ".env.appliance").read_text(encoding="utf-8")
            self.assertIn(f"CMDB_IMAGE={IMAGE_NAME}@{TARGET_DIGEST}", environment_text)
            history_paths = list((instance / "update-history").glob("*.json"))
            self.assertEqual(len(history_paths), 1)
            history = json.loads(history_paths[0].read_text(encoding="utf-8"))
            self.assertEqual(history["status"], "failed-manual-recovery-required")
            self.assertTrue(history["targetCmdbStoppedAfterFailure"])
            self.assertEqual(
                history["preMigrationRollback"],
                "prohibited-after-migration-start",
            )
        finally:
            if directory.exists():
                shutil.rmtree(directory)

    @unittest.skipUnless(
        NATIVE_POSIX_TESTS,
        "POSIX updater tests require a native POSIX host",
    )
    def test_posix_check_is_read_only(self) -> None:
        temporary_base = ROOT / "tmp"
        temporary_base.mkdir(exist_ok=True)
        directory = temporary_base / f"cmdb-update-posix-{uuid.uuid4().hex}"
        directory.mkdir()
        try:
            release = directory / "release"
            release.mkdir()
            manifest = write_release_fixture(release)
            instance = write_appliance_fixture(directory, "contract")
            environment, docker_log = fake_docker_environment(directory)
            before = hashlib.sha256((instance / ".env.appliance").read_bytes()).hexdigest()

            result = subprocess.run(
                [
                    POSIX_SHELL or "sh",
                    str(SCRIPTS / "update-cmdb.sh"),
                    "--check",
                    "--instance-name",
                    "contract",
                    "--instance-root",
                    str(instance),
                    "--manifest",
                    str(manifest),
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Status: update available.", result.stdout)
            after = hashlib.sha256((instance / ".env.appliance").read_bytes()).hexdigest()
            self.assertEqual(after, before)
            self.assertFalse((instance / ".update-cmdb.lock").exists())
            self.assertFalse((instance / "update-history").exists())
            docker_calls = docker_log.read_text(encoding="utf-8").lower()
            for forbidden in (" pull ", " stop ", " up ", " run "):
                self.assertNotIn(forbidden, f" {docker_calls} ")
        finally:
            if directory.exists():
                shutil.rmtree(directory)

    @unittest.skipUnless(
        NATIVE_POSIX_TESTS,
        "POSIX updater tests require a native POSIX host",
    )
    def test_posix_external_database_check_is_read_only(self) -> None:
        """External PostgreSQL checks must not mutate state or manage a database."""

        temporary_base = ROOT / "tmp"
        temporary_base.mkdir(exist_ok=True)
        directory = temporary_base / f"cmdb-update-posix-external-{uuid.uuid4().hex}"
        directory.mkdir()
        try:
            release = directory / "release"
            release.mkdir()
            manifest = write_release_fixture(release)
            instance = write_external_fixture(directory)
            environment, docker_log = fake_docker_environment(
                directory,
                include_postgres=False,
            )
            environment_path = instance / ".env.production"
            before = hashlib.sha256(environment_path.read_bytes()).hexdigest()

            result = subprocess.run(
                [
                    POSIX_SHELL or "sh",
                    str(SCRIPTS / "update-cmdb.sh"),
                    "--check",
                    "--instance-name",
                    "external-contract",
                    "--instance-root",
                    str(instance),
                    "--manifest",
                    str(manifest),
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Deployment mode: external", result.stdout)
            self.assertIn("Status: update available.", result.stdout)
            self.assertEqual(
                hashlib.sha256(environment_path.read_bytes()).hexdigest(),
                before,
            )
            self.assertFalse((instance / ".update-cmdb.lock").exists())
            self.assertFalse((instance / "update-history").exists())
            docker_calls = docker_log.read_text(encoding="utf-8").lower()
            self.assertNotIn("postgres:", docker_calls)
            for forbidden in (" pull ", " stop ", " up ", " run "):
                self.assertNotIn(forbidden, f" {docker_calls} ")
        finally:
            if directory.exists():
                shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()
