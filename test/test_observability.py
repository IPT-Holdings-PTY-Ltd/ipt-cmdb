import json
import logging
import os
import sys
import unittest
from unittest.mock import patch

from src.cmdb.audit import AuditContext, reset_audit_context, set_audit_context
from src.cmdb.observability import (
    JsonLogFormatter,
    TextLogFormatter,
    configure_logging,
)


class ObservabilityTests(unittest.TestCase):
    def record(self, message: str, **extra) -> logging.LogRecord:
        record = logging.LogRecord(
            "cmdb.test",
            logging.INFO,
            __file__,
            1,
            message,
            (),
            None,
        )
        for key, value in extra.items():
            setattr(record, key, value)
        return record

    def test_json_log_is_structured_and_includes_request_context(self):
        token = set_audit_context(
            AuditContext(request_id="request-42", correlation_id="correlation-42")
        )
        try:
            payload = json.loads(
                JsonLogFormatter().format(
                    self.record(
                        "HTTP request completed",
                        event="http_request_completed",
                        method="GET",
                        route="/api/assets/{asset_id}",
                        status_code=200,
                        duration_ms=12.5,
                    )
                )
            )
        finally:
            reset_audit_context(token)

        self.assertEqual(payload["event"], "http_request_completed")
        self.assertEqual(payload["request_id"], "request-42")
        self.assertEqual(payload["correlation_id"], "correlation-42")
        self.assertEqual(payload["route"], "/api/assets/{asset_id}")
        self.assertEqual(payload["status_code"], 200)
        self.assertEqual(payload["service"], "ipt-cmdb")
        self.assertTrue(payload["timestamp"].endswith("Z"))

    def test_formatter_redacts_secrets_and_ignores_arbitrary_extras(self):
        record = self.record(
            "Authorization=Bearer token-value "
            "password=credential-value "
            "postgresql://cmdb:database-password@db.internal/cmdb",
            event="security_test",
            arbitrary_payload={"privateKey": "should-not-be-serialized"},
        )

        rendered = JsonLogFormatter().format(record)

        for secret in (
            "token-value",
            "credential-value",
            "database-password",
            "should-not-be-serialized",
        ):
            self.assertNotIn(secret, rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn("arbitrary_payload", rendered)

    def test_exception_logging_retains_type_and_stack_without_message(self):
        secret = "exception-secret-value"
        try:
            raise RuntimeError(f"password={secret}")
        except RuntimeError:
            record = logging.LogRecord(
                "cmdb.test",
                logging.ERROR,
                __file__,
                1,
                "Operation failed",
                (),
                sys.exc_info(),
            )

        payload = json.loads(JsonLogFormatter().format(record))

        self.assertEqual(payload["exception_type"], "RuntimeError")
        self.assertTrue(payload["traceback"])
        self.assertNotIn(secret, json.dumps(payload))

    def test_text_format_remains_safe_and_operator_friendly(self):
        rendered = TextLogFormatter().format(
            self.record("Worker ready", event="worker_runtime", worker_name="integrations")
        )

        self.assertIn("cmdb.test", rendered)
        self.assertIn("Worker ready", rendered)
        self.assertIn("worker_name=integrations", rendered)

    def test_configure_logging_is_idempotent(self):
        with patch.dict(os.environ, {"LOG_FORMAT": "json", "LOG_LEVEL": "INFO"}):
            configure_logging()
            configure_logging()

        managed = [
            handler
            for handler in logging.getLogger("cmdb").handlers
            if getattr(handler, "_cmdb_observability_handler", False)
        ]
        self.assertEqual(len(managed), 1)
        self.assertIsInstance(managed[0].formatter, JsonLogFormatter)


if __name__ == "__main__":
    unittest.main()
