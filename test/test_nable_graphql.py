"""Tests for the bounded, tenant-safe N-able GraphQL client."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from src.cmdb.integration_reconciliation import normalize_ci_policy
from src.cmdb.nable_graphql import (
    QUERY_CATALOGUE,
    NableGraphqlClient,
    NableGraphqlConfigurationError,
    NableGraphqlRequestError,
    _RejectRedirects,
    merge_graphql_enrichment,
    normalize_graphql_endpoint,
    query_catalogue,
)


class FakeResponse:
    """Minimal context-managed JSON response."""

    def __init__(self, payload: object, status: int = 200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1):
        return json.dumps(self.payload).encode("utf-8")[:size]

    def getcode(self):
        return self.status


def asset_node(
    asset_id: str,
    customer_id: str,
    device_id: str,
    *,
    server_id: str = "server-1",
) -> dict:
    """Return one representative allow-listed GraphQL asset."""

    return {
        "id": asset_id,
        "name": f"Asset {asset_id}",
        "customer": {"id": customer_id, "name": "Acme"},
        "site": {"id": "site-1", "name": "Head office"},
        "serviceOrganization": {"id": "so-1", "name": "MSP"},
        "ncentralDevice": {"deviceId": device_id, "server": {"id": server_id}},
        "systemInfo": {
            "hostname": "APP-01",
            "manufacturer": "Dell",
            "model": "PowerEdge",
            "serialNumber": "SERIAL-1",
            "memoryTotalSizeBytes": 1024,
        },
        "operatingSystemInfo": {"name": "Windows Server", "version": "2025"},
        "agentConnection": {"status": "CONNECTED"},
        "cpu": [{"name": "Xeon", "cores": 8}],
        "chassis": {"types": ["Rack Mount"]},
        "bios": {"biosVersion": "2.4.1", "biosReleasedOn": "2026-01-02"},
        "memoryDevices": [
            {
                "location": "DIMM A1",
                "manufacturer": "Contoso Memory",
                "maxSpeedMts": 4800,
                "serialNumber": "MEM-1",
                "sizeMb": 32768,
                "type": "DDR5",
            }
        ],
        "physicalDrives": [
            {
                "diskIndex": 0,
                "model": "FastDisk",
                "serialNumber": "DISK-1",
                "sizeBytes": 1000000000,
                "type": "SSD",
            }
        ],
        "logicalDrives": [
            {
                "driveId": "C:",
                "description": "System",
                "totalSizeBytes": 900000000,
                "fileSystem": "NTFS",
            }
        ],
        "motherboard": {"manufacturer": "Dell", "model": "Board-1"},
        "networkInterfaces": [
            {
                "name": "Ethernet 1",
                "macAddress": "00:11:22:33:44:55",
                "dhcp": {"serverIpAddress": "10.0.0.1"},
                "addresses": [{"address": "10.0.0.20", "mask": "255.255.255.0", "type": "IPV4"}],
                "dnsHostName": "app-01.acme.test",
                "dnsServers": ["10.0.0.10"],
                "linkSpeedMegabitsPerSecond": 1000,
            }
        ],
        "azureVmInstance": {
            "vmId": "azure-vm-1",
            "region": "southafricanorth",
            "size": "Standard_D4s_v5",
            "subscriptionId": "subscription-1",
        },
        "reboot": {"isRequired": True},
        "vulnerabilityManagement": {
            "status": "ENABLED",
            "lastUpdatedAt": "2026-07-31T09:00:00Z",
        },
    }


class NableGraphqlClientTests(unittest.TestCase):
    """Verify allow-listing, pagination, boundaries and token-safe failures."""

    def setUp(self):
        self.configuration = {
            "graphqlEndpoint": "https://api.n-able.com/graphql",
            "graphqlApiToken": "platform-secret-token",
            "graphqlPageSize": 2,
        }

    def test_catalogue_contains_only_static_queries_and_hides_documents(self):
        public = query_catalogue()

        self.assertEqual(
            {item["key"] for item in public},
            {
                "customer_catalogue",
                "source_server_detection",
                "asset_identity",
                "asset_inventory",
                "patch_installations",
            },
        )
        self.assertTrue(all(item["readOnly"] for item in public))
        self.assertTrue(all("query" not in item for item in public))
        self.assertTrue(
            all("mutation" not in query.document.casefold() for query in QUERY_CATALOGUE.values())
        )

    def test_customer_scope_is_normalized_into_the_audited_ci_policy(self):
        policy = normalize_ci_policy(
            {"graphqlOrganizationIds": ["customer-b", " customer-a ", "customer-b"]}
        )

        self.assertEqual(
            policy["graphqlOrganizationIds"],
            ["customer-a", "customer-b"],
        )

    def test_endpoint_is_pinned_to_documented_nable_host(self):
        self.assertEqual(
            normalize_graphql_endpoint("https://api.n-able.com/graphql/"),
            "https://api.n-able.com/graphql",
        )
        for invalid in (
            "http://api.n-able.com/graphql",
            "https://evil.example/graphql",
            "https://user:secret@api.n-able.com/graphql",
            "https://api.n-able.com/graphql?token=secret",
            "https://api.n-able.com:broken/graphql",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(NableGraphqlConfigurationError):
                normalize_graphql_endpoint(invalid)

    def test_customer_catalogue_filters_non_customer_nodes_and_paginates(self):
        requests = []

        def opener(request, *, timeout):
            self.assertEqual(timeout, 20)
            requests.append(request)
            body = json.loads(request.data)
            after = body["variables"]["after"]
            if after is None:
                return FakeResponse(
                    {
                        "data": {
                            "organizationSearch": {
                                "edges": [
                                    {
                                        "cursor": "one",
                                        "node": {
                                            "id": "customer-1",
                                            "name": "Acme",
                                            "__typename": "Customer",
                                        },
                                    },
                                    {
                                        "cursor": "site",
                                        "node": {
                                            "id": "site-1",
                                            "name": "Head office",
                                            "__typename": "Site",
                                        },
                                    },
                                ],
                                "pageInfo": {"hasNextPage": True, "endCursor": "page-2"},
                            }
                        }
                    }
                )
            return FakeResponse(
                {
                    "data": {
                        "organizationSearch": {
                            "edges": [
                                {
                                    "cursor": "two",
                                    "node": {
                                        "id": "customer-2",
                                        "name": "Northwind",
                                        "__typename": "Customer",
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": "two"},
                        }
                    }
                }
            )

        result = NableGraphqlClient(self.configuration, opener=opener).test_connection()

        self.assertTrue(result["reachable"])
        self.assertEqual(
            [item["id"] for item in result["customerCandidates"]],
            ["customer-1", "customer-2"],
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer platform-secret-token")
        self.assertEqual(requests[0].get_method(), "POST")

    def test_connection_catalogue_covers_typical_msp_customer_counts(self):
        """Request the full bounded catalogue so later-page customers remain selectable."""

        client = NableGraphqlClient(self.configuration, opener=lambda *_args, **_kwargs: None)
        with patch.object(
            client,
            "customer_catalogue",
            return_value={"customers": [], "truncated": False},
        ) as catalogue:
            result = client.test_connection()

        catalogue.assert_called_once_with(limit=500)
        self.assertFalse(result["truncated"])

    def test_asset_query_uses_variables_and_builds_composite_rest_crosswalk(self):
        requests = []

        def opener(request, *, timeout):
            del timeout
            requests.append(request)
            return FakeResponse(
                {
                    "data": {
                        "assetSearch": {
                            "totalCount": 1,
                            "nodes": [asset_node("graph-1", "customer-1", "7001")],
                            "pageInfo": {"hasNextPage": False, "endCursor": "cursor-1"},
                        }
                    }
                }
            )

        result = NableGraphqlClient(self.configuration, opener=opener).assets(
            "asset_inventory", organization_ids=["customer-1"], limit=10
        )

        item = result["items"][0]
        body = json.loads(requests[0].data)
        self.assertEqual(body["variables"]["organizationIds"], ["customer-1"])
        self.assertNotIn("customer-1", body["query"])
        self.assertEqual(item["sourceIdentity"]["namespace"], "nable_graphql_asset")
        self.assertEqual(item["restIdentity"]["serverId"], "server-1")
        self.assertEqual(item["restIdentity"]["deviceId"], "7001")
        self.assertEqual(item["restIdentity"]["crosswalkKey"], "server-1:7001")
        self.assertEqual(item["summary"]["system"]["serialNumber"], "SERIAL-1")
        self.assertEqual(item["summary"]["networkInterfaces"][0]["dnsHostName"], "app-01.acme.test")
        self.assertEqual(item["summary"]["memoryDevices"][0]["sizeMb"], 32768)
        self.assertEqual(item["summary"]["azureVmInstance"]["vmId"], "azure-vm-1")
        self.assertTrue(item["summary"]["reboot"]["isRequired"])
        self.assertEqual(item["summary"]["vulnerabilityManagement"]["status"], "ENABLED")

    def test_full_asset_inventory_reads_every_cursor_page_and_deduplicates(self):
        requests = []
        unique_nodes = [
            asset_node(f"graph-{index}", "customer-1", str(7000 + index)) for index in range(105)
        ]
        pages = [
            unique_nodes[:40],
            [unique_nodes[39], *unique_nodes[40:79]],
            unique_nodes[79:],
        ]
        cursors = ["asset-page-2", "asset-page-3", None]

        def opener(request, *, timeout):
            del timeout
            body = json.loads(request.data)
            requests.append(body["variables"])
            page_index = len(requests) - 1
            return FakeResponse(
                {
                    "data": {
                        "assetSearch": {
                            "totalCount": 106,
                            "nodes": pages[page_index],
                            "pageInfo": {
                                "hasNextPage": page_index < 2,
                                "endCursor": cursors[page_index],
                            },
                        }
                    }
                }
            )

        configuration = {**self.configuration, "graphqlPageSize": 40}
        result = NableGraphqlClient(configuration, opener=opener).full_asset_inventory(
            organization_ids=["customer-1"]
        )

        self.assertEqual(result["queryKey"], "asset_inventory")
        self.assertEqual(result["organizationIds"], ["customer-1"])
        self.assertEqual(result["totalCount"], 106)
        self.assertEqual(result["pagesRead"], 3)
        self.assertFalse(result["truncated"])
        self.assertEqual(len(result["items"]), 105)
        self.assertEqual(
            [item["graphqlAssetId"] for item in result["items"]],
            [f"graph-{index}" for index in range(105)],
        )
        self.assertEqual(
            [request["after"] for request in requests],
            [None, "asset-page-2", "asset-page-3"],
        )
        self.assertTrue(all(request["first"] == 40 for request in requests))

    def test_full_asset_inventory_rejects_provider_truncation(self):
        nodes = [
            asset_node(f"graph-{index}", "customer-1", str(8000 + index)) for index in range(60)
        ]

        def opener(_request, *, timeout):
            del timeout
            return FakeResponse(
                {
                    "data": {
                        "assetSearch": {
                            "totalCount": 105,
                            "nodes": nodes,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            )

        configuration = {**self.configuration, "graphqlPageSize": 100}
        with self.assertRaisesRegex(
            NableGraphqlRequestError,
            "ended asset pagination before all reported rows were read",
        ):
            NableGraphqlClient(configuration, opener=opener).full_asset_inventory(
                organization_ids=["customer-1"]
            )

    def test_full_asset_inventory_discards_earlier_pages_when_a_later_page_errors(self):
        calls = 0
        marker = "provider-later-page-secret"

        def opener(_request, *, timeout):
            nonlocal calls
            del timeout
            calls += 1
            if calls == 1:
                return FakeResponse(
                    {
                        "data": {
                            "assetSearch": {
                                "totalCount": 105,
                                "nodes": [
                                    asset_node(f"graph-{index}", "customer-1", str(9000 + index))
                                    for index in range(100)
                                ],
                                "pageInfo": {
                                    "hasNextPage": True,
                                    "endCursor": "asset-page-2",
                                },
                            }
                        }
                    }
                )
            return FakeResponse(
                {
                    "data": {
                        "assetSearch": {
                            "totalCount": 105,
                            "nodes": [asset_node("graph-100", "customer-1", "9100")],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    },
                    "errors": [{"message": marker}],
                }
            )

        configuration = {**self.configuration, "graphqlPageSize": 100}
        with self.assertRaises(NableGraphqlRequestError) as context:
            NableGraphqlClient(configuration, opener=opener).full_asset_inventory(
                organization_ids=["customer-1"]
            )

        self.assertEqual(calls, 2)
        self.assertNotIn(marker, str(context.exception))
        self.assertNotIn("platform-secret-token", str(context.exception))

    def test_source_server_detection_returns_only_scoped_identity_candidates(self):
        requests = []
        pages = [
            {
                "data": {
                    "assetSearch": {
                        "totalCount": 4,
                        "nodes": [
                            {
                                "customer": {"id": "customer-1"},
                                "ncentralDevice": {
                                    "deviceId": "7001",
                                    "server": {"id": "server-a"},
                                },
                            },
                            {
                                "customer": {"id": "customer-1"},
                                "ncentralDevice": {
                                    "deviceId": "7002",
                                    "server": {"id": "server-a"},
                                },
                            },
                        ],
                        "pageInfo": {"hasNextPage": True, "endCursor": "page-2"},
                    }
                }
            },
            {
                "data": {
                    "assetSearch": {
                        "totalCount": 4,
                        "nodes": [
                            {
                                "customer": {"id": "customer-1"},
                                "ncentralDevice": {
                                    "deviceId": "7002",
                                    "server": {"id": "server-a"},
                                },
                            },
                            {
                                "customer": {"id": "customer-1"},
                                "ncentralDevice": {
                                    "deviceId": "8001",
                                    "server": {"id": "server-b"},
                                },
                            },
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            },
        ]

        def opener(request, *, timeout):
            del timeout
            requests.append(request)
            return FakeResponse(pages[len(requests) - 1])

        result = NableGraphqlClient(self.configuration, opener=opener).source_server_candidates(
            organization_ids=["customer-1"], limit=4
        )

        self.assertEqual(
            result["candidates"],
            [
                {
                    "serverId": "server-a",
                    "deviceIds": ["7001", "7002"],
                    "graphqlDeviceCount": 2,
                },
                {
                    "serverId": "server-b",
                    "deviceIds": ["8001"],
                    "graphqlDeviceCount": 1,
                },
            ],
        )
        self.assertFalse(result["truncated"])
        self.assertEqual(result["inspectedAssetCount"], 4)
        first_body = json.loads(requests[0].data)
        self.assertEqual(first_body["variables"]["organizationIds"], ["customer-1"])
        self.assertNotIn("systemInfo", first_body["query"])
        self.assertNotIn("serialNumber", first_body["query"])
        self.assertIn("ncentralDevice", first_body["query"])

    def test_source_server_detection_fails_closed_on_scope_escape_and_truncation(self):
        def outside_scope(_request, *, timeout):
            del timeout
            return FakeResponse(
                {
                    "data": {
                        "assetSearch": {
                            "totalCount": 1,
                            "nodes": [
                                {
                                    "customer": {"id": "customer-2"},
                                    "ncentralDevice": {
                                        "deviceId": "7001",
                                        "server": {"id": "server-a"},
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            )

        with self.assertRaises(NableGraphqlRequestError):
            NableGraphqlClient(self.configuration, opener=outside_scope).source_server_candidates(
                organization_ids=["customer-1"], limit=1
            )

        def truncated(_request, *, timeout):
            del timeout
            return FakeResponse(
                {
                    "data": {
                        "assetSearch": {
                            "totalCount": 2,
                            "nodes": [
                                {
                                    "customer": {"id": "customer-1"},
                                    "ncentralDevice": {
                                        "deviceId": "7001",
                                        "server": {"id": "server-a"},
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                        }
                    }
                }
            )

        result = NableGraphqlClient(self.configuration, opener=truncated).source_server_candidates(
            organization_ids=["customer-1"], limit=1
        )
        self.assertTrue(result["truncated"])

    def test_patch_query_is_separate_and_fails_closed_on_customer_scope(self):
        customer_id = "customer-1"

        def opener(_request, *, timeout):
            del timeout
            return FakeResponse(
                {
                    "data": {
                        "patchInstallationSearch": {
                            "totalCount": 1,
                            "nodes": [
                                {
                                    "patch": {
                                        "id": "patch-1",
                                        "name": "Security update",
                                        "patchId": "KB5000001",
                                        "severity": "CRITICAL",
                                        "isRebootRequired": True,
                                    },
                                    "asset": {
                                        "id": "graph-1",
                                        "name": "APP-01",
                                        "customer": {"id": customer_id, "name": "Acme"},
                                        "site": {"id": "site-1", "name": "HQ"},
                                    },
                                    "status": "INSTALLED",
                                    "lastUpdatedAt": "2026-07-31T09:00:00Z",
                                    "failureCount": 0,
                                    "errorDetails": [
                                        {"code": "NONE", "message": "provider-marker"}
                                    ],
                                }
                            ],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            )

        client = NableGraphqlClient(self.configuration, opener=opener)
        result = client.patch_installations(organization_ids=["customer-1"], limit=10)
        item = result["items"][0]
        self.assertEqual(item["patch"]["patchId"], "KB5000001")
        self.assertNotIn("message", item["errorDetails"][0])

        customer_id = "other-customer"
        with self.assertRaisesRegex(NableGraphqlRequestError, "outside the saved Customer"):
            client.patch_installations(organization_ids=["customer-1"], limit=10)

    def test_asset_query_fails_closed_when_provider_escapes_customer_scope(self):
        def opener(_request, *, timeout):
            del timeout
            return FakeResponse(
                {
                    "data": {
                        "assetSearch": {
                            "nodes": [asset_node("graph-1", "other-customer", "7001")],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            )

        client = NableGraphqlClient(self.configuration, opener=opener)
        with self.assertRaisesRegex(NableGraphqlRequestError, "outside the saved Customer"):
            client.assets("asset_identity", organization_ids=["customer-1"])

    def test_provider_identity_is_rejected_instead_of_truncated(self):
        def opener(_request, *, timeout):
            del timeout
            return FakeResponse(
                {
                    "data": {
                        "assetSearch": {
                            "nodes": [asset_node("x" * 161, "customer-1", "7001")],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            )

        with self.assertRaisesRegex(NableGraphqlRequestError, "invalid asset ID"):
            NableGraphqlClient(self.configuration, opener=opener).assets(
                "asset_identity", organization_ids=["customer-1"]
            )

    def test_asset_query_rejects_empty_scope_and_arbitrary_query_key(self):
        client = NableGraphqlClient(
            self.configuration,
            opener=lambda *_args, **_kwargs: self.fail("provider must not be called"),
        )
        with self.assertRaises(NableGraphqlConfigurationError):
            client.assets("asset_identity", organization_ids=[])
        with self.assertRaises(NableGraphqlConfigurationError):
            client.assets("arbitrary", organization_ids=["customer-1"])
        with self.assertRaises(NableGraphqlConfigurationError):
            client.assets("asset_identity", organization_ids=["x" * 161])

    def test_graphql_errors_and_http_bodies_do_not_expose_secrets(self):
        marker = "provider-body-secret"

        def graphql_error(_request, *, timeout):
            del timeout
            return FakeResponse(
                {
                    "errors": [
                        {
                            "message": marker,
                            "extensions": {"code": "FORBIDDEN"},
                        }
                    ],
                    "data": None,
                }
            )

        with self.assertRaises(NableGraphqlRequestError) as graphql_context:
            NableGraphqlClient(self.configuration, opener=graphql_error).test_connection()
        self.assertNotIn(marker, str(graphql_context.exception))
        self.assertNotIn("platform-secret-token", str(graphql_context.exception))
        self.assertNotIn("FORBIDDEN", str(graphql_context.exception))

        def http_error(request, *, timeout):
            del timeout
            raise HTTPError(request.full_url, 401, marker, {}, None)

        with self.assertRaises(NableGraphqlRequestError) as http_context:
            NableGraphqlClient(self.configuration, opener=http_error).test_connection()
        self.assertEqual(http_context.exception.status_code, 401)
        self.assertNotIn(marker, str(http_context.exception))

    def test_transient_http_errors_retry_static_query_with_bounded_delay(self):
        attempts = 0
        delays = []

        def opener(_request, *, timeout):
            nonlocal attempts
            del timeout
            attempts += 1
            if attempts < 3:
                raise HTTPError(
                    "https://api.n-able.com/graphql",
                    503,
                    "provider body must stay hidden",
                    {},
                    None,
                )
            return FakeResponse(
                {
                    "data": {
                        "organizationSearch": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            )

        result = NableGraphqlClient(
            self.configuration,
            opener=opener,
            sleeper=delays.append,
            jitter=lambda: 0.0,
        ).test_connection()

        self.assertTrue(result["reachable"])
        self.assertEqual(attempts, 3)
        self.assertEqual(delays, [0.25, 0.5])

    def test_retry_after_is_capped_and_non_retryable_http_errors_fail_once(self):
        attempts = 0
        delays = []

        def capped_opener(_request, *, timeout):
            nonlocal attempts
            del timeout
            attempts += 1
            if attempts == 1:
                raise HTTPError(
                    "https://api.n-able.com/graphql",
                    429,
                    "busy",
                    {"Retry-After": "120"},
                    None,
                )
            return FakeResponse(
                {
                    "data": {
                        "organizationSearch": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            )

        NableGraphqlClient(
            self.configuration,
            opener=capped_opener,
            sleeper=delays.append,
            jitter=lambda: 1.0,
        ).test_connection()
        self.assertEqual(attempts, 2)
        self.assertEqual(delays, [30.0])

        rejected_attempts = 0

        def rejected(_request, *, timeout):
            nonlocal rejected_attempts
            del timeout
            rejected_attempts += 1
            raise HTTPError(
                "https://api.n-able.com/graphql",
                400,
                "invalid query",
                {},
                None,
            )

        with self.assertRaisesRegex(NableGraphqlRequestError, "HTTP 400"):
            NableGraphqlClient(
                self.configuration,
                opener=rejected,
                sleeper=lambda _delay: self.fail("HTTP 400 must not be retried"),
            ).test_connection()
        self.assertEqual(rejected_attempts, 1)

    def test_token_whitespace_and_redirects_are_rejected_without_resend(self):
        for token in ("", "token with space", "token\r\ninjected"):
            with self.subTest(token=repr(token)), self.assertRaises(NableGraphqlConfigurationError):
                NableGraphqlClient({**self.configuration, "graphqlApiToken": token})

        handler = _RejectRedirects()
        self.assertIsNone(
            handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example")
        )

    def test_merge_requires_server_and_device_id_and_ignores_duplicates(self):
        graph = {
            "graphqlAssetId": "graph-1",
            "sourceIdentity": {"externalId": "graph-1"},
            "restIdentity": {"serverId": "server-1", "deviceId": "7001"},
            "summary": {"system": {"serialNumber": "SERIAL-1"}},
        }
        records = [
            {
                "externalId": "7001",
                "fields": {},
                "metadata": {},
                "inventoryCollections": {
                    "hardware": {"restOnly": "retained"},
                    "software": {"count": 3},
                },
            }
        ]

        merged = merge_graphql_enrichment(records, [graph], server_id="server-1")
        self.assertEqual(merged[0]["fields"]["nableGraphqlAssetId"], "graph-1")
        self.assertEqual(
            merged[0]["metadata"]["nableGraphql"]["system"]["serialNumber"],
            "SERIAL-1",
        )
        merged_again = merge_graphql_enrichment(merged, [graph], server_id="server-1")
        self.assertEqual(
            merged_again[0]["providerFingerprint"],
            merged[0]["providerFingerprint"],
        )
        self.assertEqual(
            merged_again[0]["metadata"]["nableGraphqlRestFingerprint"],
            merged[0]["metadata"]["nableGraphqlRestFingerprint"],
        )

        rich = asset_node("graph-1", "customer-1", "7001")
        normalized_rich = NableGraphqlClient(
            self.configuration,
            opener=lambda *_args, **_kwargs: FakeResponse(
                {
                    "data": {
                        "assetSearch": {
                            "nodes": [rich],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            ),
        ).assets("asset_inventory", organization_ids=["customer-1"])["items"][0]
        enriched = merge_graphql_enrichment(records, [normalized_rich], server_id="server-1")
        collections = enriched[0]["inventoryCollections"]
        self.assertEqual(collections["hardware"]["restOnly"], "retained")
        self.assertEqual(collections["software"]["count"], 3)
        self.assertEqual(collections["memory_modules"][0]["capacityBytes"], 34359738368)
        self.assertEqual(collections["network_interfaces"][0]["ipAddresses"], ["10.0.0.20"])
        self.assertEqual(collections["virtualization"]["platform"], "Microsoft Azure")
        self.assertEqual(enriched[0]["fields"]["memoryModuleCount"], 1)
        self.assertEqual(enriched[0]["fields"]["networkInterfaceCount"], 1)
        self.assertEqual(
            enriched[0]["fields"]["virtualization"]["kind"],
            "virtual_machine",
        )
        self.assertEqual(
            enriched[0]["metadata"]["sourceCoverage"]["memory_modules"]["source"],
            "nable_graphql",
        )

        duplicate = {**graph, "graphqlAssetId": "graph-2"}
        conflict = merge_graphql_enrichment(records, [graph, duplicate], server_id="server-1")
        self.assertNotIn("nableGraphqlAssetId", conflict[0]["fields"])
        wrong_server = merge_graphql_enrichment(records, [graph], server_id="server-2")
        self.assertNotIn("nableGraphqlAssetId", wrong_server[0]["fields"])


if __name__ == "__main__":
    unittest.main()
