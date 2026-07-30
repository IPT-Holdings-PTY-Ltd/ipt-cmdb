import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AzureMonitoringInfrastructureTests(unittest.TestCase):
    def setUp(self):
        self.template = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")

    def test_operational_action_group_and_alert_rules_are_provisioned(self):
        self.assertIn("Microsoft.Insights/actionGroups@2023-01-01", self.template)
        self.assertEqual(
            self.template.count("Microsoft.Insights/scheduledQueryRules@2023-12-01"),
            3,
        )
        self.assertEqual(
            self.template.count("Microsoft.Insights/metricAlerts@2018-03-01"),
            2,
        )
        self.assertIn("useCommonAlertSchema: true", self.template)

    def test_log_alerts_use_stable_structured_application_fields(self):
        for value in (
            "ContainerAppConsoleLogs_CL",
            "ContainerAppName_s == '{0}'",
            "payload.event",
            "payload.route",
            "payload.status_code",
            "'http_request_completed'",
            "'worker_runtime'",
        ):
            with self.subTest(value=value):
                self.assertIn(value, self.template)

    def test_postgres_alerts_use_supported_platform_metrics(self):
        self.assertIn("metricName: 'is_db_alive'", self.template)
        self.assertIn("metricName: 'storage_percent'", self.template)
        self.assertIn("threshold: postgresStorageAlertPercent", self.template)
        self.assertIn("param postgresStorageAlertPercent int = 80", self.template)

    def test_readiness_probe_timeout_allows_the_bounded_database_probe_to_finish(self):
        self.assertIn("path: '/api/ready'", self.template)
        self.assertIn("timeoutSeconds: 5", self.template)


if __name__ == "__main__":
    unittest.main()
