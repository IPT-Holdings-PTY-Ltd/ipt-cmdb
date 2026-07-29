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
        ):
            with self.subTest(key=key):
                self.assertIn(key, values)
                self.assertEqual(values[key], "")


if __name__ == "__main__":
    unittest.main()
