"""Tests for the bounded, read-only N-central REST API client."""

import json
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from src.cmdb.ncentral import (
    NcentralClient,
    NcentralConfigurationError,
    NcentralOperationCancelled,
    NcentralRequestError,
    normalize_base_url,
    normalize_device,
)


class FakeResponse:
    """Minimal context-managed HTTP response."""

    def __init__(self, payload, *, status=200):
        self.payload = payload
        self.status = status
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def getcode(self):
        return self.status


class NcentralClientTests(unittest.TestCase):
    """Verify token exchange, pagination, filtering and data minimization."""

    def setUp(self):
        self.configuration = {
            "baseUrl": "https://ncentral.example.com",
            "userApiToken": "permanent-user-api-token",
            "pageSize": 25,
        }

    @staticmethod
    def token_response():
        return {
            "tokens": {
                "access": {
                    "token": "temporary-access-token",
                    "type": "Bearer",
                    "expirySeconds": 3600,
                }
            }
        }

    def test_connection_exchanges_then_validates_temporary_token(self):
        requests = []

        def opener(request, *, timeout):
            del timeout
            requests.append(request)
            path = urlparse(request.full_url).path
            if path == "/api/auth/authenticate":
                return FakeResponse(self.token_response())
            if path == "/api/auth/validate":
                return FakeResponse({"message": "The token is valid."})
            if path == "/api/org-units":
                return FakeResponse(
                    {
                        "data": [
                            {
                                "orgUnitId": "101",
                                "orgUnitName": "Acme",
                                "orgUnitType": "CUSTOMER",
                            }
                        ],
                        "totalItems": 27,
                    }
                )
            raise AssertionError(path)

        result = NcentralClient(self.configuration, opener=opener).test_connection()

        self.assertTrue(result["reachable"])
        self.assertEqual(result["accessibleOrganizations"], 27)
        self.assertEqual(result["sampleOrganization"]["externalId"], "101")
        self.assertEqual(
            requests[0].get_header("Authorization"),
            "Bearer permanent-user-api-token",
        )
        self.assertEqual(
            requests[1].get_header("Authorization"),
            "Bearer temporary-access-token",
        )
        self.assertEqual(requests[0].get_method(), "POST")
        self.assertEqual(requests[1].get_method(), "GET")

    def test_customer_discovery_paginates_list_response_without_page_metadata(self):
        requested_pages = []
        requested_queries = []

        def opener(request, *, timeout):
            del timeout
            path = urlparse(request.full_url).path
            if path == "/api/auth/authenticate":
                return FakeResponse(self.token_response())
            if path == "/api/org-units":
                query = parse_qs(urlparse(request.full_url).query)
                requested_queries.append(query)
                page = int(query["pageNumber"][0])
                requested_pages.append(page)
                rows = [
                    {
                        "orgUnitId": str(index),
                        "orgUnitName": f"Customer {index}",
                        "orgUnitType": "CUSTOMER" if index % 2 else "SITE",
                        "externalId": f"EXT-{index}",
                    }
                    for index in range((page - 1) * 25 + 1, min(page * 25, 30) + 1)
                ]
                return FakeResponse({"data": rows, "totalItems": 30})
            raise AssertionError(path)

        companies = NcentralClient(self.configuration, opener=opener).discover_customers()

        self.assertEqual(requested_pages, [1, 2])
        self.assertTrue(all("select" not in query for query in requested_queries))
        self.assertTrue(
            all(
                set(query) == {"pageNumber", "pageSize", "sortBy", "sortOrder"}
                for query in requested_queries
            )
        )
        self.assertEqual(len(companies), 15)
        self.assertTrue(all(item["type"] == "CUSTOMER" for item in companies))
        self.assertNotIn("privateData", companies[0])

    def test_device_discovery_passes_native_filter_and_enriches_bounded_evidence(self):
        urls = []

        def opener(request, *, timeout):
            del timeout
            urls.append(request.full_url)
            path = urlparse(request.full_url).path
            if path == "/api/auth/authenticate":
                return FakeResponse(self.token_response())
            if path == "/api/org-units/101/devices":
                return FakeResponse(
                    {
                        "data": [
                            {
                                "deviceId": 9001,
                                "longName": "APP-SRV-01",
                                "deviceClass": "WindowsServer",
                                "deviceClassLabel": "Servers - Windows",
                                "licenseMode": "Professional",
                                "supportedOsLabel": "Windows Server 2022",
                                "customerId": "101",
                                "siteId": "102",
                                "siteName": "Head office",
                            }
                        ],
                        "pageNumber": 1,
                        "pageSize": 25,
                        "totalItems": 1,
                        "totalPages": 1,
                        "_links": {},
                    }
                )
            if path == "/api/devices/9001/assets":
                return FakeResponse(
                    {
                        "data": {
                            "computersystem": {
                                "serialnumber": "SN-9001",
                                "model": "PowerEdge",
                                "manufacturer": "Dell",
                            },
                            "networkadapter": {
                                "list": [
                                    {
                                        "ipaddress": "10.0.0.10",
                                        "macaddress": "00:11:22:33:44:55",
                                    }
                                ]
                            },
                        },
                        "_links": {},
                    }
                )
            raise AssertionError(path)

        records = NcentralClient(self.configuration, opener=opener).discover_devices(
            "101", filter_id="managed-servers", enrich_limit=1
        )

        query = parse_qs(urlparse(urls[1]).query)
        self.assertEqual(query["filterId"], ["managed-servers"])
        self.assertEqual(records[0]["externalId"], "9001")
        self.assertEqual(records[0]["identifiers"]["serial_number"], "SN-9001")
        self.assertEqual(records[0]["identifiers"]["mac_address"], "00:11:22:33:44:55")
        self.assertEqual(records[0]["fields"]["model"], "PowerEdge")
        self.assertEqual(records[0]["fields"]["ipAddress"], "10.0.0.10")
        self.assertNotIn("application", records[0])

    def test_device_asset_enrichment_is_bounded_parallel_and_order_preserving(self):
        active_reads = 0
        maximum_active_reads = 0
        lock = threading.Lock()

        def opener(request, *, timeout):
            nonlocal active_reads, maximum_active_reads
            del timeout
            path = urlparse(request.full_url).path
            if path == "/api/auth/authenticate":
                return FakeResponse(self.token_response())
            if path == "/api/org-units/101/devices":
                return FakeResponse(
                    {
                        "data": [
                            {
                                "deviceId": index,
                                "longName": f"Device {index}",
                                "deviceClass": "WindowsServer",
                            }
                            for index in range(1, 9)
                        ],
                        "pageNumber": 1,
                        "pageSize": 25,
                        "totalItems": 8,
                        "totalPages": 1,
                    }
                )
            if path.startswith("/api/devices/") and path.endswith("/assets"):
                with lock:
                    active_reads += 1
                    maximum_active_reads = max(maximum_active_reads, active_reads)
                time.sleep(0.02)
                with lock:
                    active_reads -= 1
                device_id = path.split("/")[3]
                return FakeResponse(
                    {"data": {"computersystem": {"serialnumber": f"SN-{device_id}"}}}
                )
            raise AssertionError(path)

        records = NcentralClient(self.configuration, opener=opener).discover_devices(
            "101", enrich_limit=8
        )

        self.assertGreaterEqual(maximum_active_reads, 2)
        self.assertLessEqual(maximum_active_reads, 4)
        self.assertEqual(
            [record["externalId"] for record in records],
            [str(index) for index in range(1, 9)],
        )
        self.assertEqual(
            [record["fields"]["serialNumber"] for record in records],
            [f"SN-{index}" for index in range(1, 9)],
        )

    def test_device_progress_is_ordered_and_counts_every_enrichment(self):
        active_reads = 0
        maximum_active_reads = 0
        progress = []
        lock = threading.Lock()

        def opener(request, *, timeout):
            nonlocal active_reads, maximum_active_reads
            del timeout
            path = urlparse(request.full_url).path
            if path == "/api/auth/authenticate":
                return FakeResponse(self.token_response())
            if path == "/api/org-units/101/devices":
                return FakeResponse(
                    {
                        "data": [
                            {
                                "deviceId": index,
                                "longName": f"Device {index}",
                                "deviceClass": "WindowsServer",
                            }
                            for index in range(1, 7)
                        ],
                        "pageNumber": 1,
                        "pageSize": 25,
                        "totalItems": 6,
                        "totalPages": 1,
                    }
                )
            if path.startswith("/api/devices/") and path.endswith("/assets"):
                device_id = int(path.split("/")[3])
                with lock:
                    active_reads += 1
                    maximum_active_reads = max(maximum_active_reads, active_reads)
                time.sleep(0.005 * (7 - device_id))
                with lock:
                    active_reads -= 1
                return FakeResponse(
                    {"data": {"computersystem": {"serialnumber": f"SN-{device_id}"}}}
                )
            raise AssertionError(path)

        records = NcentralClient(self.configuration, opener=opener).discover_devices(
            "101",
            enrich_limit=6,
            progress_callback=progress.append,
        )

        self.assertLessEqual(maximum_active_reads, 4)
        self.assertEqual(
            [record["externalId"] for record in records],
            [str(index) for index in range(1, 7)],
        )
        self.assertEqual(progress[0]["phase"], "discovering")
        enriching = [event for event in progress if event["phase"] == "enriching"]
        self.assertEqual([event["current"] for event in enriching], list(range(7)))
        self.assertTrue(all(event["total"] == 6 for event in enriching))
        self.assertTrue(all(event["discovered"] == 6 for event in enriching))
        self.assertEqual(enriching[-1]["enriched"], 6)

    def test_cancellation_stops_submitting_additional_enrichment_requests(self):
        active_reads = 0
        maximum_active_reads = 0
        asset_requests = []
        cancel = threading.Event()
        lock = threading.Lock()

        def opener(request, *, timeout):
            nonlocal active_reads, maximum_active_reads
            del timeout
            path = urlparse(request.full_url).path
            if path == "/api/auth/authenticate":
                return FakeResponse(self.token_response())
            if path == "/api/org-units/101/devices":
                return FakeResponse(
                    {
                        "data": [
                            {
                                "deviceId": index,
                                "longName": f"Device {index}",
                                "deviceClass": "WindowsServer",
                            }
                            for index in range(1, 13)
                        ],
                        "pageNumber": 1,
                        "pageSize": 25,
                        "totalItems": 12,
                        "totalPages": 1,
                    }
                )
            if path.startswith("/api/devices/") and path.endswith("/assets"):
                device_id = int(path.split("/")[3])
                with lock:
                    asset_requests.append(device_id)
                    active_reads += 1
                    maximum_active_reads = max(maximum_active_reads, active_reads)
                time.sleep(0.01 if device_id == 1 else 0.05)
                with lock:
                    active_reads -= 1
                return FakeResponse(
                    {"data": {"computersystem": {"serialnumber": f"SN-{device_id}"}}}
                )
            raise AssertionError(path)

        def on_progress(event):
            if event["phase"] == "enriching" and event["current"] == 1:
                cancel.set()

        with self.assertRaises(NcentralOperationCancelled):
            NcentralClient(self.configuration, opener=opener).discover_devices(
                "101",
                enrich_limit=12,
                progress_callback=on_progress,
                cancel_requested=cancel.is_set,
            )

        self.assertLessEqual(maximum_active_reads, 4)
        self.assertGreaterEqual(len(asset_requests), 1)
        self.assertLessEqual(len(asset_requests), 4)
        self.assertTrue(set(asset_requests).issubset({1, 2, 3, 4}))

    def test_normalizer_accepts_documented_direct_asset_example(self):
        result = normalize_device(
            {"deviceId": 1, "longName": "NC-1"},
            {
                "computersystem": {"serialnumber": "SERIAL-1"},
                "networkadapter": {"list": [{"macaddress": "AA:BB:CC:DD:EE:FF"}]},
            },
        )
        self.assertEqual(result["identifiers"]["serial_number"], "SERIAL-1")
        self.assertEqual(result["identifiers"]["mac_address"], "AA:BB:CC:DD:EE:FF")

    def test_configuration_and_provider_failures_never_expose_token_or_body(self):
        with self.assertRaises(NcentralConfigurationError):
            normalize_base_url("http://ncentral.example.com?token=secret")
        with self.assertRaisesRegex(NcentralConfigurationError, "userApiToken"):
            NcentralClient({**self.configuration, "userApiToken": ""})

        def opener(request, *, timeout):
            del timeout
            raise HTTPError(
                request.full_url,
                401,
                "permanent-user-api-token provider body",
                {},
                None,
            )

        with self.assertRaisesRegex(NcentralRequestError, "HTTP 401") as caught:
            NcentralClient(self.configuration, opener=opener).test_connection()
        self.assertEqual(caught.exception.status_code, 401)
        self.assertNotIn("permanent-user-api-token", str(caught.exception))
        self.assertNotIn("provider body", str(caught.exception))

    def test_http_429_emits_concurrency_telemetry(self):
        observations = []

        def opener(request, *, timeout):
            del timeout
            raise HTTPError(request.full_url, 429, "busy", {"Retry-After": "12"}, None)

        with self.assertRaises(NcentralRequestError):
            NcentralClient(
                self.configuration,
                opener=opener,
                telemetry_callback=observations.append,
            ).test_connection()
        self.assertTrue(observations[0]["limited"])
        self.assertEqual(observations[0]["retryAfterSeconds"], 12)
        self.assertEqual(observations[0]["requestPath"], "/api/auth/authenticate")
        self.assertNotIn("userApiToken", observations[0])

    def test_success_telemetry_is_coalesced_during_multi_request_read(self):
        observations = []

        def opener(request, *, timeout):
            del timeout
            path = urlparse(request.full_url).path
            if path == "/api/auth/authenticate":
                return FakeResponse(self.token_response())
            if path == "/api/auth/validate":
                return FakeResponse({"message": "The token is valid."})
            if path == "/api/org-units":
                return FakeResponse({"data": [], "totalItems": 0})
            raise AssertionError(path)

        NcentralClient(
            self.configuration,
            opener=opener,
            telemetry_callback=observations.append,
        ).test_connection()

        self.assertEqual(len(observations), 1)
        self.assertFalse(observations[0]["limited"])


if __name__ == "__main__":
    unittest.main()
