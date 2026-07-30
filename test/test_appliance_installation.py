"""Contract tests for safe, repeatable compact-appliance installation."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
POWERSHELL = shutil.which("pwsh")
POSIX_SHELL = shutil.which("sh")
OPENSSL = shutil.which("openssl")
DOCKER = shutil.which("docker")
TEST_IMAGE = (
    "ghcr.io/ipt-holdings-pty-ltd/ipt-cmdb@sha256:"
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
)
TAGGED_TEST_IMAGE = TEST_IMAGE.replace(
    "@sha256:",
    ":v0.3.0@sha256:",
)


def dotenv_values(path: Path) -> dict[str, str]:
    """Read non-comment assignments from one generated environment file."""

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    return values


def file_digest(path: Path) -> str:
    """Return a stable digest without exposing generated secret values."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


class ApplianceScriptContractTests(unittest.TestCase):
    """Keep installer entry points aligned with the hardened Compose contract."""

    def test_initializers_require_public_origin_and_immutable_image(self) -> None:
        powershell = (SCRIPTS / "Initialize-Appliance.ps1").read_text(encoding="utf-8")
        shell = (SCRIPTS / "initialize-appliance.sh").read_text(encoding="utf-8")

        for source in (powershell, shell):
            with self.subTest(script="PowerShell" if source is powershell else "POSIX"):
                self.assertIn("PUBLIC_BASE_URL=", source)
                self.assertIn("sha256:", source)
                self.assertIn("latest", source)
                self.assertIn("already exists", source)
        self.assertNotIn("[switch]$Force", powershell)
        self.assertNotIn("ipt-cmdb:latest", powershell)
        self.assertNotIn("ipt-cmdb:latest", shell)

    def test_guided_installers_use_wait_and_preserve_existing_instances(self) -> None:
        powershell = (SCRIPTS / "Install-Cmdb.ps1").read_text(encoding="utf-8")
        shell = (SCRIPTS / "install-cmdb.sh").read_text(encoding="utf-8")

        for source in (powershell, shell):
            with self.subTest(script="PowerShell" if source is powershell else "POSIX"):
                self.assertIn("release-manifest.txt", source)
                self.assertIn("release-manifest.json", source)
                self.assertIn("pull", source)
                self.assertIn("--wait", source)
                self.assertIn("/api/live", source)
                self.assertIn("/api/ready", source)
                self.assertIn("will not be regenerated", source)
                self.assertNotIn("logs -f", source)

    @unittest.skipUnless(POWERSHELL, "PowerShell 7 is not available")
    def test_powershell_initializer_is_atomic_and_rerun_preserves_secrets(self) -> None:
        temporary_base = ROOT / "tmp"
        temporary_base.mkdir(exist_ok=True)
        instance = temporary_base / f"cmdb-appliance-{uuid.uuid4().hex}"
        try:
            command = [
                POWERSHELL or "pwsh",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(SCRIPTS / "Initialize-Appliance.ps1"),
                "-AdminEmail",
                "OWNER@EXAMPLE.COM",
                "-PublicBaseUrl",
                "https://cmdb.example.test/",
                "-InstanceName",
                "customer-contract",
                "-Image",
                TAGGED_TEST_IMAGE,
                "-Port",
                "3043",
                "-InstanceRoot",
                str(instance),
            ]
            first = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
            if first.returncode and (
                "Unable to restrict the appliance instance directory" in first.stderr
                or "Access is denied" in first.stderr
            ):
                self.skipTest("The test host blocks child-process Windows ACL changes")
            self.assertEqual(first.returncode, 0, first.stderr)

            environment = dotenv_values(instance / ".env.appliance")
            self.assertEqual(environment["PUBLIC_BASE_URL"], "https://cmdb.example.test")
            self.assertEqual(environment["CMDB_IMAGE"], TAGGED_TEST_IMAGE)
            self.assertEqual(
                environment["CMDB_IMAGE_DIGEST"],
                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            )
            self.assertEqual(environment["BOOTSTRAP_ADMIN_EMAIL"], "owner@example.com")
            self.assertEqual(environment["AUTH_MODE"], "local")
            self.assertEqual(environment["LOCAL_MFA_POLICY"], "all")
            self.assertEqual(environment["CMDB_BIND_ADDRESS"], "127.0.0.1")
            self.assertEqual(environment["CMDB_MIGRATION_LOCK_TIMEOUT_MS"], "60000")
            self.assertEqual(environment["CMDB_MIGRATION_STATEMENT_TIMEOUT_MS"], "900000")

            secret_paths = sorted((instance / "secrets").glob("*.txt"))
            self.assertEqual(len(secret_paths), 4)
            self.assertTrue(all(path.stat().st_size >= 32 for path in secret_paths))
            before = {path.name: file_digest(path) for path in secret_paths}

            second = subprocess.run(
                command, capture_output=True, text=True, timeout=30, check=False
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("never replaced", second.stderr)
            after = {path.name: file_digest(path) for path in secret_paths}
            self.assertEqual(after, before)

            if DOCKER:
                compose_version = subprocess.run(
                    [DOCKER, "compose", "version"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                if compose_version.returncode == 0:
                    rendered = subprocess.run(
                        [
                            DOCKER,
                            "compose",
                            "--env-file",
                            str(instance / ".env.appliance"),
                            "-f",
                            str(ROOT / "compose.appliance.yml"),
                            "config",
                            "--quiet",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    self.assertEqual(rendered.returncode, 0, rendered.stderr)
        finally:
            if instance.exists():
                shutil.rmtree(instance)

    @unittest.skipUnless(POWERSHELL, "PowerShell 7 is not available")
    def test_powershell_initializer_rejects_unsafe_inputs_without_writing(self) -> None:
        temporary_base = ROOT / "tmp"
        temporary_base.mkdir(exist_ok=True)
        cases = (
            ("http://cmdb.example.test", TEST_IMAGE, "invalid-origin"),
            (
                "https://cmdb.example.test",
                "ghcr.io/ipt-holdings-pty-ltd/ipt-cmdb:v1.2.3",
                "production-semver",
            ),
            (
                "https://cmdb.example.test",
                "ghcr.io/ipt-holdings-pty-ltd/ipt-cmdb:latest",
                "mutable-image",
            ),
            (
                "https://cmdb.example.test",
                "ghcr.io/ipt-holdings-pty-ltd/ipt-cmdb$INJECT:v1.2.3",
                "unsafe-image",
            ),
        )
        for public_base_url, image, name in cases:
            with self.subTest(name=name):
                instance = temporary_base / f"cmdb-{name}-{uuid.uuid4().hex}"
                try:
                    result = subprocess.run(
                        [
                            POWERSHELL or "pwsh",
                            "-NoProfile",
                            "-NonInteractive",
                            "-File",
                            str(SCRIPTS / "Initialize-Appliance.ps1"),
                            "-AdminEmail",
                            "owner@example.com",
                            "-PublicBaseUrl",
                            public_base_url,
                            "-InstanceName",
                            name,
                            "-Image",
                            image,
                            "-InstanceRoot",
                            str(instance),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(instance.exists())
                finally:
                    if instance.exists():
                        shutil.rmtree(instance)

    @unittest.skipUnless(POWERSHELL, "PowerShell 7 is not available")
    def test_guided_powershell_whatif_does_not_create_secrets_or_need_docker(self) -> None:
        temporary_base = ROOT / "tmp"
        temporary_base.mkdir(exist_ok=True)
        instance = temporary_base / f"cmdb-install-plan-{uuid.uuid4().hex}"
        try:
            environment = dict(os.environ)
            environment["PATH"] = ""
            result = subprocess.run(
                [
                    POWERSHELL or "pwsh",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(SCRIPTS / "Install-Cmdb.ps1"),
                    "-AdminEmail",
                    "owner@example.com",
                    "-PublicBaseUrl",
                    "https://cmdb.example.test",
                    "-InstanceName",
                    "planned-instance",
                    "-Image",
                    TEST_IMAGE,
                    "-InstanceRoot",
                    str(instance),
                    "-WhatIf",
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("What if", result.stdout)
            self.assertFalse(instance.exists())
        finally:
            if instance.exists():
                shutil.rmtree(instance)

    @unittest.skipUnless(POWERSHELL, "PowerShell 7 is not available")
    def test_guided_powershell_accepts_legacy_tagged_digest_manifest(self) -> None:
        temporary_base = ROOT / "tmp"
        temporary_base.mkdir(exist_ok=True)
        bundle = temporary_base / f"cmdb-bundle-{uuid.uuid4().hex}"
        instance = temporary_base / f"cmdb-manifest-plan-{uuid.uuid4().hex}"
        try:
            bundle_scripts = bundle / "scripts"
            bundle_scripts.mkdir(parents=True)
            shutil.copy2(SCRIPTS / "Install-Cmdb.ps1", bundle_scripts)
            (bundle / "release-manifest.txt").write_text(
                "\n".join(
                    (
                        "IPT CMDB release: v0.3.0",
                        "Commit: " + ("b" * 40),
                        "Container: ghcr.io/ipt-holdings-pty-ltd/ipt-cmdb:v0.3.0",
                        "Digest: sha256:" + ("a" * 64),
                        "",
                    )
                ),
                encoding="utf-8",
            )
            environment = dict(os.environ)
            environment["PATH"] = ""
            result = subprocess.run(
                [
                    POWERSHELL or "pwsh",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(bundle_scripts / "Install-Cmdb.ps1"),
                    "-AdminEmail",
                    "owner@example.com",
                    "-PublicBaseUrl",
                    "https://cmdb.example.test",
                    "-InstanceRoot",
                    str(instance),
                    "-WhatIf",
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("What if", result.stdout)
            self.assertFalse(instance.exists())
        finally:
            if bundle.exists():
                shutil.rmtree(bundle)
            if instance.exists():
                shutil.rmtree(instance)

    @unittest.skipUnless(
        POSIX_SHELL and OPENSSL,
        "A POSIX shell and OpenSSL are not available",
    )
    def test_posix_initializer_rerun_preserves_secrets(self) -> None:
        instance_name = f"contract-{uuid.uuid4().hex[:10]}"
        instance = ROOT / ".appliance" / instance_name
        command = [
            POSIX_SHELL or "sh",
            str(SCRIPTS / "initialize-appliance.sh"),
            "owner@example.com",
            instance_name,
            "https://cmdb.example.test",
            TAGGED_TEST_IMAGE,
            "3044",
        ]
        try:
            first = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            environment = dotenv_values(instance / ".env.appliance")
            self.assertEqual(environment["CMDB_IMAGE"], TAGGED_TEST_IMAGE)
            self.assertEqual(
                environment["CMDB_IMAGE_DIGEST"],
                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            )
            self.assertEqual(environment["CMDB_MIGRATION_LOCK_TIMEOUT_MS"], "60000")
            self.assertEqual(environment["CMDB_MIGRATION_STATEMENT_TIMEOUT_MS"], "900000")
            secret_paths = sorted((instance / "secrets").glob("*.txt"))
            before = {path.name: file_digest(path) for path in secret_paths}

            second = subprocess.run(
                command, capture_output=True, text=True, timeout=30, check=False
            )
            self.assertNotEqual(second.returncode, 0)
            after = {path.name: file_digest(path) for path in secret_paths}
            self.assertEqual(after, before)
        finally:
            if instance.exists():
                shutil.rmtree(instance)


if __name__ == "__main__":
    unittest.main()
