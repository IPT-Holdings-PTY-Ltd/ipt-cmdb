import unittest
from datetime import date, timedelta

from src.cmdb.notifications import (
    notification_candidates,
    notification_dedupe_key,
    render_notification_template,
    resolve_notification_recipients,
)


class NotificationDomainTests(unittest.TestCase):
    def setUp(self):
        self.rule = {
            "id": "rule-1",
            "key": "asset-renewal",
            "eventType": "asset_renewal",
            "leadDays": 30,
            "recipientRoles": ["business_owner", "technical_owner"],
            "fallbackAddresses": [],
        }
        self.asset = {
            "id": "asset-1",
            "companyId": "acme",
            "name": "Sage 200 subscription",
            "type": "Software",
            "status": "Active",
            "metadata": {"renewalDate": (date.today() + timedelta(days=14)).isoformat()},
        }

    def test_lifecycle_candidate_and_dedupe_are_deterministic(self):
        candidates = notification_candidates(
            self.rule,
            [self.asset],
            [],
            [{"id": "acme", "name": "Acme Manufacturing"}],
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["context"]["days"], 14)
        self.assertEqual(
            notification_dedupe_key(self.rule, candidates[0]),
            f"asset-renewal:asset:asset-1:{self.asset['metadata']['renewalDate']}",
        )

    def test_recipient_resolution_respects_preferences_and_reports_gaps(self):
        candidate = {
            "eventType": "asset_renewal",
            "assetIds": ["asset-1"],
        }
        responsibilities = [
            {
                "assetId": "asset-1",
                "contactId": "owner-1",
                "contactEmail": "owner@example.com",
                "role": "business_owner",
                "effectiveUntil": None,
            },
            {
                "assetId": "asset-1",
                "contactId": "tech-1",
                "contactEmail": "tech@example.com",
                "role": "technical_owner",
                "effectiveUntil": None,
            },
        ]
        recipients, missing = resolve_notification_recipients(
            self.rule,
            candidate,
            responsibilities,
            [{"contactId": "owner-1", "emailEnabled": False, "eventTypes": ["*"]}],
        )
        self.assertEqual(recipients, ["tech@example.com"])
        self.assertEqual(missing, ["business_owner"])

    def test_template_context_is_escaped_for_html(self):
        rendered = render_notification_template(
            {
                "subjectTemplate": "Renew {{asset_name}}",
                "htmlTemplate": "<p>{{asset_name}}</p>",
                "textTemplate": "{{asset_name}}",
            },
            {"asset_name": "Sage <script>alert(1)</script>"},
        )
        self.assertNotIn("<script>", rendered["bodyHtml"])
        self.assertIn("&lt;script&gt;", rendered["bodyHtml"])
        self.assertEqual(rendered["subject"], "Renew Sage <script>alert(1)</script>")


if __name__ == "__main__":
    unittest.main()
