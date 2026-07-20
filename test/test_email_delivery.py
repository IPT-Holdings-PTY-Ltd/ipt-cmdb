import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from src.cmdb.email_delivery import (
    GraphEmailSender,
    exchange_rbac_script,
    graph_message_payload,
    public_email_connection,
    valid_email_address,
)


class FakeCredential:
    def get_token(self, _scope):
        return type("Token", (), {"token": "access-token"})()


class FakeResponse:
    def __init__(self, status=202, request_id="graph-request"):
        self.status = status
        self.headers = {"request-id": request_id}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status


class EmailDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.connection = {
            "enabled": True,
            "authMode": "client_secret",
            "tenantId": "tenant",
            "clientId": "client",
            "senderAddress": "cmdb@example.com",
            "replyTo": "helpdesk@example.com",
        }
        self.message = {
            "to": ["tech@example.com"],
            "subject": "CMDB test",
            "bodyHtml": "<p>Ready</p>",
        }

    def test_connection_response_never_contains_encrypted_secret(self):
        public = public_email_connection(
            {
                **self.connection,
                "clientSecretEncrypted": "ciphertext",
                "clientSecretNonce": "nonce",
            }
        )
        self.assertTrue(public["hasClientSecret"])
        self.assertNotIn("clientSecretEncrypted", public)
        self.assertNotIn("clientSecretNonce", public)

    def test_graph_payload_and_address_validation(self):
        self.assertTrue(valid_email_address("tech@example.com"))
        self.assertFalse(valid_email_address("not-an-address"))
        payload = graph_message_payload(self.message, self.connection)
        self.assertEqual(payload["message"]["body"]["contentType"], "HTML")
        self.assertEqual(
            payload["message"]["replyTo"][0]["emailAddress"]["address"],
            "helpdesk@example.com",
        )

    @patch("src.cmdb.email_delivery.build_credential", return_value=FakeCredential())
    def test_graph_acceptance_returns_request_identifier(self, _credential):
        captured = []

        def opener(request, timeout):
            captured.append((request, timeout))
            return FakeResponse()

        result = GraphEmailSender(opener=opener).send(
            self.connection, self.message, client_secret="secret"
        )
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.provider_request_id, "graph-request")
        self.assertIn("/users/cmdb@example.com/sendMail", captured[0][0].full_url)
        self.assertNotIn("secret", captured[0][0].data.decode("utf-8"))

    @patch("src.cmdb.email_delivery.build_credential", return_value=FakeCredential())
    def test_graph_throttling_observes_retry_after(self, _credential):
        calls = []
        sleeps = []

        def opener(request, timeout):
            calls.append((request, timeout))
            if len(calls) == 1:
                raise HTTPError(
                    request.full_url,
                    429,
                    "throttled",
                    {"Retry-After": "2"},
                    io.BytesIO(),
                )
            return FakeResponse()

        GraphEmailSender(opener=opener, sleeper=sleeps.append).send(
            self.connection, self.message, client_secret="secret"
        )
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(len(calls), 2)

    def test_exchange_script_contains_scope_but_no_secret(self):
        script = exchange_rbac_script(self.connection)
        self.assertIn("Application Mail.Send", script)
        self.assertIn("cmdb@example.com", script)
        self.assertNotIn("client_secret", script)


if __name__ == "__main__":
    unittest.main()
