"""Regression tests for security-sensitive Compose environment forwarding."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_LINE = re.compile(r"^ {6}([A-Z][A-Z0-9_]*):\s*(.*?)\s*$")
NCENTRAL_ENVIRONMENT = {
    "NCENTRAL_BASE_URL": "${NCENTRAL_BASE_URL:-}",
    "NCENTRAL_USER_API_TOKEN": "${NCENTRAL_USER_API_TOKEN:-}",
    "NCENTRAL_USER_API_TOKEN_FILE": "${NCENTRAL_USER_API_TOKEN_FILE:-}",
    "NCENTRAL_API_TOKEN": "${NCENTRAL_API_TOKEN:-}",
    "NCENTRAL_PAGE_SIZE": "${NCENTRAL_PAGE_SIZE:-250}",
    "NCENTRAL_GRAPHQL_ENABLED": "${NCENTRAL_GRAPHQL_ENABLED:-false}",
    "NCENTRAL_GRAPHQL_ENDPOINT": ("${NCENTRAL_GRAPHQL_ENDPOINT:-https://api.n-able.com/graphql}"),
    "NCENTRAL_GRAPHQL_API_TOKEN": "${NCENTRAL_GRAPHQL_API_TOKEN:-}",
    "NCENTRAL_GRAPHQL_API_TOKEN_FILE": "${NCENTRAL_GRAPHQL_API_TOKEN_FILE:-}",
    "NCENTRAL_GRAPHQL_PAGE_SIZE": "${NCENTRAL_GRAPHQL_PAGE_SIZE:-100}",
    "NCENTRAL_GRAPHQL_SERVER_ID": "${NCENTRAL_GRAPHQL_SERVER_ID:-}",
}
LOCAL_LOGIN_ENVIRONMENT = {
    "FORWARDED_ALLOW_IPS": "${FORWARDED_ALLOW_IPS:-}",
    "LOCAL_LOGIN_IDENTIFIER_LIMIT": "${LOCAL_LOGIN_IDENTIFIER_LIMIT:-5}",
    "LOCAL_LOGIN_IDENTIFIER_WINDOW_SECONDS": ("${LOCAL_LOGIN_IDENTIFIER_WINDOW_SECONDS:-900}"),
    "LOCAL_LOGIN_SOURCE_LIMIT": "${LOCAL_LOGIN_SOURCE_LIMIT:-20}",
    "LOCAL_LOGIN_SOURCE_WINDOW_SECONDS": "${LOCAL_LOGIN_SOURCE_WINDOW_SECONDS:-900}",
    "LOCAL_LOGIN_PENDING_TTL_SECONDS": "${LOCAL_LOGIN_PENDING_TTL_SECONDS:-120}",
    "LOCAL_LOGIN_THROTTLE_AUDIT_SECONDS": ("${LOCAL_LOGIN_THROTTLE_AUDIT_SECONDS:-300}"),
}
OBSERVABILITY_ENVIRONMENT = {
    "LOG_FORMAT": "${LOG_FORMAT:-json}",
    "LOG_LEVEL": "${LOG_LEVEL:-INFO}",
}
MIGRATION_ENVIRONMENT = {
    "CMDB_MIGRATION_LOCK_TIMEOUT_MS": "${CMDB_MIGRATION_LOCK_TIMEOUT_MS:-60000}",
    "CMDB_MIGRATION_STATEMENT_TIMEOUT_MS": ("${CMDB_MIGRATION_STATEMENT_TIMEOUT_MS:-900000}"),
}


def service_environment(path: Path, service: str) -> dict[str, str]:
    """Return one service's statically declared Compose environment mapping."""

    lines = path.read_text(encoding="utf-8").splitlines()
    service_header = f"  {service}:"
    try:
        service_start = lines.index(service_header)
    except ValueError as error:
        raise AssertionError(f"{path.name} does not define service {service!r}") from error

    environment_start: int | None = None
    for index in range(service_start + 1, len(lines)):
        line = lines[index]
        if line.startswith("  ") and not line.startswith("    ") and line.strip():
            break
        if line == "    environment:":
            environment_start = index + 1
            break
    if environment_start is None:
        raise AssertionError(f"{path.name}:{service} has no environment mapping")

    environment: dict[str, str] = {}
    for line in lines[environment_start:]:
        stripped = line.strip()
        indentation = len(line) - len(line.lstrip())
        if stripped and not stripped.startswith("#") and indentation <= 4:
            break
        match = ENVIRONMENT_LINE.match(line)
        if match:
            environment[match.group(1)] = match.group(2)
    return environment


def dotenv_values(path: Path) -> dict[str, str]:
    """Read non-comment assignments from a checked-in environment example."""

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    return values


class ComposeNcentralEnvironmentTests(unittest.TestCase):
    """Keep all supported runtime services on one N-central environment contract."""

    def test_ncentral_environment_is_forwarded_to_every_runtime_service(self) -> None:
        deployments = (
            ("docker-compose.yml", "cmdb"),
            ("compose.production.yml", "cmdb"),
            ("compose.appliance.yml", "cmdb"),
            ("compose.worker.yml", "worker"),
        )

        for filename, service in deployments:
            with self.subTest(filename=filename, service=service):
                environment = service_environment(ROOT / filename, service)
                for key, expected in NCENTRAL_ENVIRONMENT.items():
                    self.assertEqual(environment.get(key), expected)

    def test_production_example_does_not_store_ncentral_token_values(self) -> None:
        values = dotenv_values(ROOT / ".env.production.example")

        self.assertIn("NCENTRAL_BASE_URL", values)
        self.assertEqual(values["NCENTRAL_PAGE_SIZE"], "250")
        for key in (
            "NCENTRAL_USER_API_TOKEN",
            "NCENTRAL_USER_API_TOKEN_FILE",
            "NCENTRAL_API_TOKEN",
            "NCENTRAL_GRAPHQL_API_TOKEN",
            "NCENTRAL_GRAPHQL_API_TOKEN_FILE",
        ):
            with self.subTest(key=key):
                self.assertIn(key, values)
                self.assertEqual(values[key], "")
        self.assertEqual(values["NCENTRAL_GRAPHQL_ENABLED"], "false")
        self.assertEqual(values["NCENTRAL_GRAPHQL_PAGE_SIZE"], "100")
        self.assertEqual(values["NCENTRAL_GRAPHQL_SERVER_ID"], "")
        self.assertEqual(
            values["NCENTRAL_GRAPHQL_ENDPOINT"],
            "https://api.n-able.com/graphql",
        )
        self.assertEqual(values["NCENTRAL_GRAPHQL_PAGE_SIZE"], "100")
        self.assertEqual(values["NCENTRAL_GRAPHQL_SERVER_ID"], "")

    def test_local_secret_folder_is_excluded_from_container_build_context(self) -> None:
        """Prevent locally staged provider tokens from entering an image layer."""

        ignored = {
            line.strip().rstrip("/")
            for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertIn("appliance-secrets", ignored)


class AuthenticationDeploymentContractTests(unittest.TestCase):
    """Keep local-login protection explicit in every HTTP deployment profile."""

    def test_login_security_environment_is_forwarded_to_web_services(self) -> None:
        deployments = (
            ("docker-compose.yml", "cmdb"),
            ("compose.production.yml", "cmdb"),
            ("compose.appliance.yml", "cmdb"),
        )

        for filename, service in deployments:
            with self.subTest(filename=filename):
                environment = service_environment(ROOT / filename, service)
                for key, expected in LOCAL_LOGIN_ENVIRONMENT.items():
                    self.assertEqual(environment.get(key), expected)

    def test_environment_examples_never_enable_unbounded_proxy_trust(self) -> None:
        for filename in (".env.example", ".env.production.example"):
            with self.subTest(filename=filename):
                values = dotenv_values(ROOT / filename)
                self.assertIn("FORWARDED_ALLOW_IPS", values)
                self.assertNotIn(values["FORWARDED_ALLOW_IPS"], {"*", "0.0.0.0/0", "::/0"})
                for key in LOCAL_LOGIN_ENVIRONMENT:
                    self.assertIn(key, values)

    def test_container_enables_maintained_proxy_middleware_without_wildcard(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn('"--proxy-headers"', dockerfile)
        self.assertIn('"--no-access-log"', dockerfile)
        self.assertNotIn("FORWARDED_ALLOW_IPS=*", dockerfile)


class ObservabilityDeploymentContractTests(unittest.TestCase):
    """Keep structured application logs consistent across container roles."""

    def test_observability_environment_is_forwarded_to_every_runtime_service(self) -> None:
        deployments = (
            ("docker-compose.yml", "cmdb"),
            ("compose.production.yml", "cmdb"),
            ("compose.appliance.yml", "cmdb"),
            ("compose.worker.yml", "worker"),
        )

        for filename, service in deployments:
            with self.subTest(filename=filename, service=service):
                environment = service_environment(ROOT / filename, service)
                for key, expected in OBSERVABILITY_ENVIRONMENT.items():
                    self.assertEqual(environment.get(key), expected)

    def test_environment_examples_document_logging_controls(self) -> None:
        for filename in (".env.example", ".env.production.example"):
            with self.subTest(filename=filename):
                values = dotenv_values(ROOT / filename)
                self.assertEqual(values["LOG_FORMAT"], "json")
                self.assertEqual(values["LOG_LEVEL"], "INFO")


class WorkerDeploymentContractTests(unittest.TestCase):
    """Keep the non-HTTP worker compatible with Compose update waits."""

    def test_worker_disables_the_web_image_healthcheck(self) -> None:
        worker_compose = (ROOT / "compose.worker.yml").read_text(encoding="utf-8")

        self.assertRegex(
            worker_compose,
            r"(?ms)^  worker:\n.*?^    healthcheck:\n      disable: true$",
        )

    def test_migration_timeouts_are_forwarded_to_every_runtime_service(self) -> None:
        deployments = (
            ("docker-compose.yml", "cmdb"),
            ("compose.production.yml", "cmdb"),
            ("compose.appliance.yml", "cmdb"),
            ("compose.worker.yml", "worker"),
        )

        for filename, service in deployments:
            with self.subTest(filename=filename, service=service):
                environment = service_environment(ROOT / filename, service)
                for key, expected in MIGRATION_ENVIRONMENT.items():
                    self.assertEqual(environment.get(key), expected)


if __name__ == "__main__":
    unittest.main()
