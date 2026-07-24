"""Tests for bounded, read-only ConnectWise company discovery."""

import base64
import json
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from src.cmdb.connectwise import (
    ConnectWiseClient,
    ConnectWiseConfigurationError,
    ConnectWiseRequestError,
    normalize_base_url,
    normalize_configuration,
)


class FakeResponse:
    """Minimal context-managed HTTP response used by the client tests."""

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ConnectWiseClientTests(unittest.TestCase):
    """Verify pagination, authentication shape and the least-data boundary."""

    def setUp(self):
        self.configuration = {
            "baseUrl": "https://api-na.myconnectwise.net/v4_6_release/apis/3.0",
            "companyId": "ipt",
            "publicKey": "public-key",
            "privateKey": "private-key",
            "clientId": "client-id",
            "pageSize": 25,
        }

    def test_discovery_pages_deduplicates_and_sanitizes_provider_data(self):
        requests = []

        def opener(request, *, timeout):
            requests.append((request, timeout))
            page = int(parse_qs(urlparse(request.full_url).query)["page"][0])
            if page == 1:
                rows = [
                    {
                        "id": index,
                        "identifier": f"C{index:03d}",
                        "name": f"Customer {index}",
                        "status": {"name": "Active"},
                        "type": {"name": "Customer"},
                        "site": {"name": "Head office"},
                        "territory": {"name": "IPT - Pretoria"},
                        "privateNote": "must-never-be-stored",
                    }
                    for index in range(1, 26)
                ]
            else:
                rows = [
                    {
                        "id": 25,
                        "identifier": "C025",
                        "name": "Customer 25",
                        "secret": "discard-me",
                    },
                    {
                        "id": 26,
                        "identifier": "C026",
                        "name": "Customer 26",
                        "deletedFlag": True,
                    },
                ]
            return FakeResponse(rows)

        companies = ConnectWiseClient(self.configuration, opener=opener).discover_companies(
            page_size=25
        )

        self.assertEqual(len(companies), 26)
        self.assertEqual(companies[0]["site"], "IPT - Pretoria")
        self.assertTrue(companies[-1]["deleted"])
        self.assertNotIn("privateNote", companies[0])
        self.assertNotIn("secret", companies[-1])
        self.assertEqual(len(requests), 2)
        request = requests[0][0]
        expected = base64.b64encode(b"ipt+public-key:private-key").decode("ascii")
        self.assertEqual(request.get_header("Authorization"), f"Basic {expected}")
        self.assertEqual(request.get_header("Clientid"), "client-id")
        self.assertEqual(request.get_method(), "GET")

    def test_full_discovery_uses_large_least_data_pages_by_default(self):
        """Discovery minimizes provider round trips and response payload size."""

        urls = []

        def opener(request, *, timeout):
            del timeout
            urls.append(request.full_url)
            return FakeResponse(
                [
                    {
                        "id": 1,
                        "identifier": "ACME",
                        "name": "Acme",
                        "territory": {"name": "IPT - Pretoria"},
                    }
                ]
            )

        companies = ConnectWiseClient(self.configuration, opener=opener).discover_companies()

        query = parse_qs(urlparse(urls[0]).query)
        self.assertEqual(len(companies), 1)
        self.assertEqual(query["pageSize"], ["1000"])
        self.assertEqual(
            query["fields"],
            ["id,identifier,name,status,types,territory,deletedFlag,lastUpdated"],
        )

    def test_connection_uses_a_single_record_read(self):
        urls = []

        def opener(request, *, timeout):
            del timeout
            urls.append(request.full_url)
            return FakeResponse([{"id": 7, "identifier": "ACME", "name": "Acme"}])

        result = ConnectWiseClient(self.configuration, opener=opener).test_connection()

        self.assertTrue(result["reachable"])
        self.assertEqual(result["sampleCompany"]["externalId"], "7")
        self.assertEqual(parse_qs(urlparse(urls[0]).query)["pageSize"], ["1"])

    def test_preview_uses_one_bounded_page_and_accepts_company_territory(self):
        urls = []

        def opener(request, *, timeout):
            del timeout
            urls.append(request.full_url)
            return FakeResponse(
                [
                    {
                        "id": index,
                        "identifier": f"C{index:03d}",
                        "name": f"Customer {index}",
                        "territory": {"name": "Gauteng"},
                        "types": [{"name": "Customer"}, {"name": "Managed service"}],
                    }
                    for index in range(1, 51)
                ]
            )

        companies, truncated = ConnectWiseClient(
            self.configuration, opener=opener
        ).preview_companies(limit=50)

        self.assertEqual(len(companies), 50)
        self.assertEqual(companies[0]["site"], "Gauteng")
        self.assertEqual(companies[0]["type"], "Customer, Managed service")
        self.assertEqual(companies[0]["typeValues"], ["Customer", "Managed service"])
        self.assertTrue(truncated)
        self.assertEqual(len(urls), 1)
        self.assertEqual(parse_qs(urlparse(urls[0]).query)["pageSize"], ["50"])

    def test_territory_catalogue_uses_the_dedicated_read_only_collection(self):
        """Territory choices are complete rather than inferred from company sites."""

        urls = []

        def opener(request, *, timeout):
            del timeout
            urls.append(request.full_url)
            return FakeResponse(
                [
                    {"territory": {"id": 2, "name": "Pronto - Pretoria"}},
                    {"territory": {"id": 1, "name": "IPT - Cape Town"}},
                    {"territory": {"id": 1, "name": "IPT - Cape Town"}},
                ]
            )

        territories = ConnectWiseClient(self.configuration, opener=opener).list_territories()

        self.assertEqual(territories, ["IPT - Cape Town", "Pronto - Pretoria"])
        self.assertTrue(urlparse(urls[0]).path.endswith("/company/companies"))
        self.assertEqual(parse_qs(urlparse(urls[0]).query)["pageSize"], ["1000"])
        self.assertEqual(parse_qs(urlparse(urls[0]).query)["fields"], ["territory"])

    def test_configuration_discovery_is_company_scoped_and_sanitized(self):
        """Only mapped-company configurations and reviewed fields leave the adapter."""

        urls = []

        def opener(request, *, timeout):
            del timeout
            urls.append(request.full_url)
            return FakeResponse(
                [
                    {
                        "id": 41,
                        "name": "APP-SRV-01",
                        "type": {"id": 11, "name": "Server"},
                        "status": {"id": 3, "name": "Managed"},
                        "company": {"id": 250, "name": "Acme"},
                        "site": {"name": "Head office"},
                        "serialNumber": "SN-41",
                        "modelNumber": "PowerEdge",
                        "ipAddress": "10.0.0.41",
                        "vendorNotes": "not retained",
                        "questions": [{"answer": "not retained"}],
                        "_info": {"lastUpdated": "2026-07-21T10:00:00Z"},
                    }
                ]
            )

        items = ConnectWiseClient(self.configuration, opener=opener).discover_configurations("250")

        query = parse_qs(urlparse(urls[0]).query)
        self.assertEqual(query["conditions"], ["company/id=250"])
        self.assertEqual(query["pageSize"], ["1000"])
        self.assertEqual(items[0]["identifiers"]["serial_number"], "SN-41")
        self.assertEqual(items[0]["metadata"]["site"], "Head office")
        self.assertEqual(items[0]["providerTypeId"], "11")
        self.assertEqual(items[0]["providerTypeName"], "Server")
        self.assertEqual(items[0]["providerStatusId"], "3")
        self.assertEqual(items[0]["providerStatusName"], "Managed")
        self.assertNotIn("vendorNotes", items[0])
        self.assertNotIn("questions", items[0])
        with self.assertRaises(ConnectWiseConfigurationError):
            ConnectWiseClient(self.configuration, opener=opener).discover_configurations(
                "250 OR id>0"
            )

    def test_configuration_normalizer_requires_provider_identity(self):
        with self.assertRaises(ConnectWiseRequestError):
            normalize_configuration({"name": "Missing ID"})

    def test_configuration_rejects_unsafe_or_incomplete_urls(self):
        unsafe_urls = [
            "http://api.example.com/v4_6_release/apis/3.0",
            "https://user:pass@api.example.com/api",
            "https://api.example.com/api?token=secret",
        ]
        for value in unsafe_urls:
            with self.subTest(value=value), self.assertRaises(ConnectWiseConfigurationError):
                normalize_base_url(value)
        with self.assertRaisesRegex(ConnectWiseConfigurationError, "privateKey"):
            ConnectWiseClient({**self.configuration, "privateKey": ""})

    def test_http_error_does_not_expose_body_or_credentials(self):
        def opener(request, *, timeout):
            del timeout
            raise HTTPError(request.full_url, 401, "Unauthorized private-key", {}, None)

        with self.assertRaisesRegex(ConnectWiseRequestError, "HTTP 401") as caught:
            ConnectWiseClient(self.configuration, opener=opener).test_connection()
        message = str(caught.exception)
        self.assertNotIn("private-key", message)
        self.assertNotIn("Unauthorized", message)


if __name__ == "__main__":
    unittest.main()
