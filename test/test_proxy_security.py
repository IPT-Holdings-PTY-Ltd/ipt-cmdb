"""Trusted-proxy regression tests for authentication source attribution."""

import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import backend.main as backend_main


def proxy_test_client(peer: str, trusted_hosts: list[str]) -> TestClient:
    """Return a minimal app using the same maintained proxy middleware as Uvicorn."""

    application = FastAPI()

    @application.get("/")
    def resolved_source(request: Request) -> dict[str, str]:
        return {"client": backend_main._canonical_client_address(request)}

    return TestClient(
        ProxyHeadersMiddleware(application, trusted_hosts=trusted_hosts),
        client=(peer, 50000),
    )


class TrustedProxyTests(unittest.TestCase):
    """Ensure caller-controlled forwarding data cannot evade authentication controls."""

    def test_untrusted_peer_cannot_override_client_with_forwarded_header(self):
        client = proxy_test_client("203.0.113.8", ["10.42.0.0/23"])

        response = client.get("/", headers={"X-Forwarded-For": "198.51.100.22"})

        self.assertEqual(response.json()["client"], "203.0.113.8")

    def test_trusted_chain_selects_nearest_untrusted_address_from_the_right(self):
        client = proxy_test_client("10.42.0.4", ["10.42.0.0/23"])

        response = client.get(
            "/",
            headers={"X-Forwarded-For": ("192.0.2.99, 198.51.100.22, 10.42.0.5")},
        )

        self.assertEqual(response.json()["client"], "198.51.100.22")

    def test_malformed_forwarded_address_fails_into_one_unknown_bucket(self):
        client = proxy_test_client("10.42.0.4", ["10.42.0.0/23"])

        response = client.get("/", headers={"X-Forwarded-For": "not-an-ip-address"})

        self.assertEqual(response.json()["client"], "unknown")

    def test_unsafe_or_invalid_proxy_configuration_is_rejected(self):
        for value in ("*", "0.0.0.0/0", "::/0", "not-a-network"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"FORWARDED_ALLOW_IPS": value}),
                self.assertRaisesRegex(RuntimeError, "FORWARDED_ALLOW_IPS"),
            ):
                backend_main._validate_forwarded_proxy_trust()


if __name__ == "__main__":
    unittest.main()
