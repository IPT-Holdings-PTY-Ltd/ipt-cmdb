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

    def test_rich_normalizer_is_case_insensitive_bounded_and_secret_safe(self):
        result = normalize_device(
            {
                "DEVICEID": 7,
                "LONGNAME": "HV-01",
                "DeviceClass": "WindowsServer",
                "LicenseMode": "Professional",
                "DeviceStatusLabel": "Normal",
                "LastApplianceCheckinTime": "2026-07-31T09:30:00Z",
                "LastLoggedInUser": "DOMAIN\\sensitive-user",
                "RemoteControlUri": "https://remote.example/secret",
            },
            {
                "DATA": {
                    "ComputerSystem": {
                        "Manufacturer": "Microsoft Corporation",
                        "Model": "Virtual Machine",
                        "SerialNumber": "SERIAL-7",
                        "SystemUUID": "uuid-7",
                        "TotalPhysicalMemory": 34359738368,
                    },
                    "OperatingSystem": {
                        "ReportedOS": "Windows Server 2022",
                        "OSArchitecture": "64-bit",
                        "Version": "10.0.20348",
                        "LastBootUpTime": "2026-07-30T12:00:00Z",
                    },
                },
                "_ExTrA": {
                    "NetworkAdapter": {
                        "LiSt": [
                            {
                                "Name": "Ethernet 2",
                                "IPAddress": ["10.0.0.8", "fe80::8"],
                                "MACAddress": "00:11:22:33:44:66",
                                "DefaultGateway": "10.0.0.1",
                                "DNSServers": ["10.0.0.2", "10.0.0.3"],
                                "DHCPEnabled": False,
                            },
                            {
                                "Name": "Ethernet 1",
                                "IPAddress": "192.168.1.8",
                                "MACAddress": "00:11:22:33:44:55",
                            },
                        ]
                    },
                    "PROCESSORS": {
                        "items": [
                            {
                                "Name": "Intel Xeon",
                                "NumberOfCores": 8,
                                "NumberOfLogicalProcessors": 16,
                            }
                        ]
                    },
                    "PhysicalMemory": {
                        "list": [
                            {
                                "Manufacturer": "Contoso",
                                "PartNumber": "MEM-1",
                                "Capacity": 17179869184,
                                "Speed": 3200,
                            }
                        ]
                    },
                    "DiskDrives": {
                        "list": [
                            {
                                "Model": "Virtual Disk",
                                "SerialNumber": "DISK-1",
                                "Size": 536870912000,
                            }
                        ]
                    },
                    "Volumes": {
                        "list": [
                            {
                                "DeviceId": "C:",
                                "VolumeName": "System",
                                "FileSystem": "NTFS",
                                "Size": 500000000000,
                                "FreeSpace": 200000000000,
                            }
                        ]
                    },
                    "Applications": {
                        "list": [
                            {
                                "DisplayName": "Sage 200",
                                "Publisher": "Sage",
                                "DisplayVersion": "2026.1",
                                "ProductKey": "NEVER-STORE-THIS-KEY",
                                "InstallLocation": "C:\\Secret\\Path",
                            },
                            {
                                "DisplayName": "SQL Server",
                                "Publisher": "Microsoft",
                                "Version": "2022",
                            },
                        ]
                    },
                    "WindowsFeatures": {
                        "list": [{"DisplayName": "Hyper-V", "InstallState": "Installed"}]
                    },
                    "ServerRoles": {
                        "list": [{"DisplayName": "Web Server (IIS)", "State": "Enabled"}]
                    },
                    "ServiceMonitorStatus": {
                        "list": [
                            {
                                "ServiceName": "Agent health",
                                "Status": "Normal",
                                "ServiceAccount": "DOMAIN\\svc-secret",
                                "ExecutablePath": "C:\\Secret\\agent.exe",
                            }
                        ]
                    },
                    "Lifecycle-Info": {
                        "WarrantyExpiryDate": "2027-06-30",
                        "LeaseExpiryDate": "2028-06-30",
                        "ExpectedReplacementDate": "2029-06-30",
                        "PurchaseDate": "2024-06-30",
                        "AssetTag": "HV-0007",
                    },
                },
            },
        )

        inventory = result["inventoryCollections"]
        self.assertEqual(result["status"], "Normal")
        self.assertEqual(result["metadata"]["operationalStatus"], "healthy")
        self.assertEqual(result["metadata"]["lifecycle"], "in_service")
        self.assertEqual(result["fields"]["ncentralLicenseMode"], "Professional")
        self.assertEqual(result["fields"]["deviceIdentifier"], "uuid-7")
        self.assertNotIn("lastLoggedInUser", result["fields"])
        self.assertEqual(len(inventory["network_interfaces"]), 2)
        self.assertEqual(inventory["processors"][0]["cores"], 8)
        self.assertEqual(inventory["memory_modules"][0]["capacityBytes"], 17179869184)
        self.assertEqual(inventory["physical_disks"][0]["serialNumber"], "DISK-1")
        self.assertEqual(inventory["volumes"][0]["freeBytes"], 200000000000)
        self.assertEqual(inventory["software"]["count"], 2)
        self.assertEqual(len(inventory["server_features"]), 1)
        self.assertEqual(len(inventory["server_roles"]), 1)
        self.assertEqual(inventory["monitoring"]["count"], 1)
        self.assertEqual(inventory["lifecycle"]["leaseEnd"], "2028-06-30")
        self.assertEqual(result["fields"]["virtualization"]["kind"], "virtual_machine")
        self.assertEqual(result["fields"]["virtualization"]["platform"], "Hyper-V")
        self.assertEqual(result["metadata"]["warrantyEnd"], "2027-06-30")
        self.assertEqual(
            result["metadata"]["sourceCoverage"]["network_interfaces"]["recordCount"],
            2,
        )
        serialized = json.dumps(result)
        for sensitive in (
            "sensitive-user",
            "remote.example",
            "NEVER-STORE-THIS-KEY",
            "C:\\Secret\\Path",
            "svc-secret",
            "agent.exe",
        ):
            self.assertNotIn(sensitive, serialized)

    def test_virtualization_retains_only_explicit_provider_relationship_ids(self):
        result = normalize_device(
            {
                "deviceId": 19,
                "longName": "VM-19",
                "deviceClass": "WindowsServer",
            },
            {
                "data": {
                    "virtualization": {
                        "role": "Virtual Machine",
                        "platform": "Hyper-V",
                        "hostName": "HV-01",
                        "hostDeviceId": 7,
                        "clusterDeviceId": 80,
                    }
                }
            },
        )

        virtualization = result["fields"]["virtualization"]
        self.assertEqual(virtualization["kind"], "virtual_machine")
        self.assertEqual(virtualization["hostExternalId"], "7")
        self.assertEqual(virtualization["clusterExternalId"], "80")
        self.assertEqual(
            virtualization["hostEvidence"],
            ["explicit_provider_device_id"],
        )

    def test_absent_detail_sections_do_not_emit_authoritative_empty_inventory(self):
        base = {
            "deviceId": 77,
            "longName": "Server 77",
            "deviceClass": "WindowsServer",
        }

        partial = normalize_device(base)
        explicit_empty = normalize_device(
            base,
            {
                "data": {
                    "application": {"list": []},
                    "windowsFeatures": {"list": []},
                }
            },
        )

        self.assertNotIn("software", partial["inventoryCollections"])
        self.assertNotIn("server_features", partial["inventoryCollections"])
        self.assertEqual(
            partial["metadata"]["sourceCoverage"]["software"]["state"],
            "not_reported",
        )
        self.assertEqual(explicit_empty["inventoryCollections"]["software"]["count"], 0)
        self.assertEqual(explicit_empty["inventoryCollections"]["server_features"], [])
        self.assertEqual(
            explicit_empty["metadata"]["sourceCoverage"]["software"]["state"],
            "available",
        )

    def test_live_os_capability_shape_is_separate_from_roles_and_features(self):
        result = normalize_device(
            {
                "deviceId": 78,
                "longName": "Server 78",
                "deviceClass": "WindowsServer",
            },
            {
                "data": {
                    "_extra": {
                        "osfeatures": {
                            "list": [
                                {"pkey": "PowerShellVersion", "pvalue": "5.1"},
                                {"pkey": "PowerShell-SnapIn-0", "pvalue": "Core"},
                                {"pkey": "Password", "pvalue": "NEVER-STORE"},
                            ]
                        }
                    }
                }
            },
        )

        self.assertNotIn("server_roles", result["inventoryCollections"])
        self.assertNotIn("server_features", result["inventoryCollections"])
        self.assertEqual(
            [item["name"] for item in result["inventoryCollections"]["os_capabilities"]],
            ["PowerShell-SnapIn-0", "PowerShellVersion"],
        )
        self.assertNotIn("NEVER-STORE", json.dumps(result))

    def test_hyperv_virtual_switch_adapter_is_explainable_host_evidence(self):
        result = normalize_device(
            {
                "deviceId": 79,
                "longName": "HV-79",
                "deviceClass": "WindowsServer",
            },
            {
                "data": {
                    "computerSystem": {
                        "manufacturer": "Supermicro",
                        "model": "Physical Server",
                    },
                    "networkAdapter": {
                        "list": [
                            {
                                "name": "vEthernet (Intel I210 - Virtual Switch)",
                                "description": "Hyper-V Virtual Ethernet Adapter",
                            }
                        ]
                    },
                }
            },
        )

        virtualization = result["inventoryCollections"]["virtualization"]
        self.assertEqual(virtualization["kind"], "hypervisor_host")
        self.assertEqual(virtualization["platform"], "Hyper-V")
        self.assertEqual(virtualization["confidence"], "medium")
        self.assertEqual(virtualization["evidence"], ["hyperv_virtual_switch_adapter"])

        guest_adapter_only = normalize_device(
            {
                "deviceId": 80,
                "longName": "Guest 80",
                "deviceClass": "WindowsServer",
            },
            {
                "data": {
                    "networkAdapter": {
                        "list": [{"description": "Microsoft Hyper-V Network Adapter"}]
                    }
                }
            },
        )
        self.assertNotIn("virtualization", guest_adapter_only["inventoryCollections"])

    def test_provider_fingerprint_excludes_checkin_but_tracks_inventory_changes(self):
        base_record = {
            "deviceId": 8,
            "longName": "APP-08",
            "deviceStatus": "Online",
            "lastApplianceCheckinTime": "2026-07-31T08:00:00Z",
        }
        asset = {
            "data": {
                "computerSystem": {
                    "serialNumber": "SERIAL-8",
                    "model": "PowerEdge R650",
                }
            }
        }
        first = normalize_device(base_record, asset)
        second = normalize_device(
            {
                **base_record,
                "lastApplianceCheckinTime": "2026-07-31T09:00:00Z",
            },
            asset,
        )
        changed = normalize_device(
            base_record,
            {
                "data": {
                    "computerSystem": {
                        "serialNumber": "SERIAL-8",
                        "model": "PowerEdge R660",
                    }
                }
            },
        )

        self.assertEqual(first["providerVersion"], second["providerVersion"])
        self.assertNotEqual(first["providerVersion"], changed["providerVersion"])
        self.assertEqual(first["providerFingerprint"], first["providerVersion"])
        self.assertTrue(first["providerVersion"].startswith("sha256:"))

    def test_one_asset_read_failure_does_not_abort_device_discovery(self):
        progress = []

        def opener(request, *, timeout):
            del timeout
            path = urlparse(request.full_url).path
            if path == "/api/auth/authenticate":
                return FakeResponse(self.token_response())
            if path == "/api/org-units/101/devices":
                return FakeResponse(
                    {
                        "data": [
                            {"deviceId": 1, "longName": "Device 1"},
                            {"deviceId": 2, "longName": "Device 2"},
                        ],
                        "pageNumber": 1,
                        "pageSize": 25,
                        "totalItems": 2,
                        "totalPages": 1,
                    }
                )
            if path == "/api/devices/1/assets":
                return FakeResponse({"data": {"computerSystem": {"serialNumber": "SN-1"}}})
            if path == "/api/devices/2/assets":
                raise HTTPError(request.full_url, 503, "busy", {}, None)
            raise AssertionError(path)

        records = NcentralClient(self.configuration, opener=opener).discover_devices(
            "101",
            enrich_limit=2,
            progress_callback=progress.append,
        )

        self.assertEqual([record["externalId"] for record in records], ["1", "2"])
        self.assertEqual(records[0]["fields"]["serialNumber"], "SN-1")
        self.assertNotIn("serialNumber", records[1]["fields"])
        self.assertEqual(progress[-1]["failed"], 1)
        self.assertEqual(progress[-1]["enriched"], 1)

    def test_enrichment_rotation_and_priority_are_explicit_and_bounded(self):
        requested_assets = []

        def opener(request, *, timeout):
            del timeout
            path = urlparse(request.full_url).path
            if path == "/api/auth/authenticate":
                return FakeResponse(self.token_response())
            if path == "/api/org-units/101/devices":
                return FakeResponse(
                    {
                        "data": [
                            {"deviceId": index, "longName": f"Device {index}"}
                            for index in range(1, 7)
                        ],
                        "pageNumber": 1,
                        "pageSize": 25,
                        "totalItems": 6,
                        "totalPages": 1,
                    }
                )
            if path.startswith("/api/devices/") and path.endswith("/assets"):
                requested_assets.append(int(path.split("/")[3]))
                return FakeResponse({"data": {}})
            raise AssertionError(path)

        NcentralClient(self.configuration, opener=opener).discover_devices(
            "101",
            enrich_limit=3,
            enrichment_offset=3,
            priority_external_ids=["2"],
        )

        self.assertEqual(set(requested_assets), {2, 4, 5})
        self.assertEqual(len(requested_assets), 3)

    def test_full_enrichment_reads_bounded_operational_inventory_without_custom_values(self):
        paths = []

        def opener(request, *, timeout):
            del timeout
            path = urlparse(request.full_url).path
            paths.append(path)
            if path == "/api/auth/authenticate":
                return FakeResponse(self.token_response())
            if path == "/api/org-units/101/devices":
                return FakeResponse(
                    {
                        "data": [{"deviceId": 42, "longName": "HV-42"}],
                        "pageNumber": 1,
                        "pageSize": 25,
                        "totalItems": 1,
                        "totalPages": 1,
                    }
                )
            if path == "/api/org-units/101/active-issues":
                return FakeResponse(
                    {
                        "data": [
                            {
                                "deviceId": 42,
                                "serviceId": 900,
                                "serviceName": "Patch status",
                                "stateStatus": "Warning",
                            }
                        ],
                        "pageNumber": 1,
                        "pageSize": 25,
                        "totalItems": 1,
                        "totalPages": 1,
                    }
                )
            if path == "/api/devices/42/assets":
                return FakeResponse(
                    {
                        "data": {
                            "computerSystem": {
                                "manufacturer": "Dell",
                                "model": "PowerEdge",
                            }
                        }
                    }
                )
            if path == "/api/devices/42/assets/lifecycle-info":
                return FakeResponse(
                    {
                        "data": {
                            "warrantyExpiryDate": "2028-01-31",
                            "expectedReplacementDate": "2029-01-31",
                        }
                    }
                )
            if path == "/api/devices/42/service-monitor-status":
                return FakeResponse(
                    {
                        "data": [
                            {
                                "serviceId": 901,
                                "serviceName": "Agent health",
                                "stateStatus": "Normal",
                            }
                        ]
                    }
                )
            if path == "/api/devices/42/maintenance-windows":
                return FakeResponse(
                    {
                        "data": [
                            {
                                "id": 77,
                                "name": "Monthly patching",
                                "enabled": True,
                                "durationMinutes": 120,
                            }
                        ]
                    }
                )
            raise AssertionError(path)

        records = NcentralClient(self.configuration, opener=opener).discover_devices(
            "101",
            enrich_limit=250,
        )

        inventory = records[0]["inventoryCollections"]
        self.assertEqual(inventory["lifecycle"]["warrantyEnd"], "2028-01-31")
        self.assertEqual(inventory["monitoring"]["count"], 2)
        self.assertEqual(
            inventory["monitoring"]["stateCounts"],
            {"Normal": 1, "Warning": 1},
        )
        self.assertEqual(inventory["maintenance_windows"][0]["durationMinutes"], 120)
        self.assertNotIn("/api/devices/42/custom-properties", paths)

    def test_full_enrichment_serializes_provider_lifecycle_reads(self):
        active_lifecycle_reads = 0
        peak_lifecycle_reads = 0
        lifecycle_lock = threading.Lock()

        def opener(request, *, timeout):
            nonlocal active_lifecycle_reads, peak_lifecycle_reads
            del timeout
            path = urlparse(request.full_url).path
            if path == "/api/auth/authenticate":
                return FakeResponse(self.token_response())
            if path == "/api/org-units/101/devices":
                return FakeResponse(
                    {
                        "data": [
                            {"deviceId": index, "longName": f"Device {index}"}
                            for index in range(1, 5)
                        ],
                        "pageNumber": 1,
                        "pageSize": 25,
                        "totalItems": 4,
                        "totalPages": 1,
                    }
                )
            if path == "/api/org-units/101/active-issues":
                return FakeResponse(
                    {
                        "data": [],
                        "pageNumber": 1,
                        "pageSize": 25,
                        "totalItems": 0,
                        "totalPages": 1,
                    }
                )
            if path.endswith("/assets"):
                return FakeResponse({"data": {}})
            if path.endswith("/assets/lifecycle-info"):
                with lifecycle_lock:
                    active_lifecycle_reads += 1
                    peak_lifecycle_reads = max(
                        peak_lifecycle_reads,
                        active_lifecycle_reads,
                    )
                time.sleep(0.02)
                with lifecycle_lock:
                    active_lifecycle_reads -= 1
                return FakeResponse({"data": {}})
            if path.endswith("/service-monitor-status"):
                return FakeResponse({"data": []})
            if path.endswith("/maintenance-windows"):
                return FakeResponse({"data": []})
            raise AssertionError(path)

        NcentralClient(self.configuration, opener=opener).discover_devices(
            "101",
            enrich_limit=250,
        )

        self.assertEqual(peak_lifecycle_reads, 1)

    def test_capability_probe_is_bounded_read_only_and_returns_no_values(self):
        paths = []

        def opener(request, *, timeout):
            del timeout
            path = urlparse(request.full_url).path
            paths.append(path)
            if path == "/api/auth/authenticate":
                return FakeResponse(self.token_response())
            if path == "/api/org-units/101/devices":
                return FakeResponse(
                    {
                        "data": [{"deviceId": 42, "longName": "SECRET-DEVICE-NAME"}],
                        "pageNumber": 1,
                        "pageSize": 1,
                        "totalItems": 1,
                        "totalPages": 1,
                    }
                )
            if path == "/api/devices/42":
                return FakeResponse(
                    {
                        "data": {
                            "deviceId": 42,
                            "customerId": 101,
                            "longName": "SECRET-DEVICE-NAME",
                        }
                    }
                )
            if path == "/api/devices/42/assets":
                return FakeResponse(
                    {
                        "computerSystem": {"serialNumber": "SECRET-SERIAL"},
                        "application": {"list": [{"productKey": "SECRET-PRODUCT-KEY"}]},
                    }
                )
            if path == "/api/devices/42/assets/lifecycle-info":
                return FakeResponse({"cost": 999999, "assetTag": "SECRET-ASSET-TAG"})
            if path == "/api/devices/42/custom-properties":
                return FakeResponse(
                    {
                        "data": [
                            {
                                "propertyName": "DB Password",
                                "propertyType": "PASSWORD",
                                "value": "SECRET-CUSTOM-PROPERTY",
                            }
                        ]
                    }
                )
            if path == "/api/devices/42/service-monitor-status":
                return FakeResponse({"data": [{"serviceName": "Private service"}]})
            if path == "/api/devices/42/maintenance-windows":
                return FakeResponse({"data": [{"name": "Confidential window"}]})
            if path == "/api/org-units/101/active-issues":
                return FakeResponse({"data": [{"deviceName": "SECRET-DEVICE-NAME"}]})
            raise AssertionError(path)

        result = NcentralClient(self.configuration, opener=opener).probe_device_capabilities(
            "101",
            sample_size=1,
        )

        self.assertTrue(result["readOnly"])
        self.assertEqual(result["sampleCount"], 1)
        self.assertLessEqual(result["sampleCount"], 3)
        serialized = json.dumps(result)
        for sensitive in (
            "SECRET-DEVICE-NAME",
            "SECRET-SERIAL",
            "SECRET-PRODUCT-KEY",
            "SECRET-ASSET-TAG",
            "DB Password",
            "SECRET-CUSTOM-PROPERTY",
            "Private service",
            "Confidential window",
        ):
            self.assertNotIn(sensitive, serialized)
        self.assertNotIn("propertyName", serialized)
        self.assertEqual(
            result["samples"][0]["endpoints"]["assets"]["accessStatus"],
            "available",
        )
        self.assertTrue(all(path.startswith("/api/") for path in paths))

    def test_configuration_and_provider_failures_never_expose_token_or_body(self):
        with self.assertRaises(NcentralConfigurationError):
            normalize_base_url("http://ncentral.example.com?token=secret")
        with self.assertRaisesRegex(NcentralConfigurationError, "userApiToken"):
            NcentralClient({**self.configuration, "userApiToken": ""})

        request_count = 0

        def opener(request, *, timeout):
            nonlocal request_count
            del timeout
            request_count += 1
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
        self.assertEqual(request_count, 1)
        self.assertNotIn("permanent-user-api-token", str(caught.exception))
        self.assertNotIn("provider body", str(caught.exception))

    def test_http_429_emits_concurrency_telemetry(self):
        observations = []
        delays = []

        def opener(request, *, timeout):
            del timeout
            raise HTTPError(request.full_url, 429, "busy", {"Retry-After": "12"}, None)

        with self.assertRaises(NcentralRequestError):
            NcentralClient(
                self.configuration,
                opener=opener,
                telemetry_callback=observations.append,
                sleeper=delays.append,
                jitter=lambda: 0.0,
            ).test_connection()
        self.assertEqual(len(observations), 3)
        self.assertTrue(observations[0]["limited"])
        self.assertEqual(observations[0]["retryAfterSeconds"], 12)
        self.assertEqual(observations[0]["requestPath"], "/api/auth/authenticate")
        self.assertNotIn("userApiToken", observations[0])
        self.assertEqual(delays, [12.0, 12.0])

    def test_transient_get_retries_with_bounded_exponential_delay(self):
        attempts = {"validate": 0}
        delays = []

        def opener(request, *, timeout):
            del timeout
            path = urlparse(request.full_url).path
            if path == "/api/auth/authenticate":
                return FakeResponse(self.token_response())
            if path == "/api/auth/validate":
                attempts["validate"] += 1
                if attempts["validate"] < 3:
                    raise HTTPError(request.full_url, 503, "busy", {}, None)
                return FakeResponse({"message": "The token is valid."})
            if path == "/api/org-units":
                return FakeResponse({"data": [], "totalItems": 0})
            raise AssertionError(path)

        result = NcentralClient(
            self.configuration,
            opener=opener,
            sleeper=delays.append,
            jitter=lambda: 0.0,
        ).test_connection()

        self.assertTrue(result["reachable"])
        self.assertEqual(attempts["validate"], 3)
        self.assertEqual(delays, [0.25, 0.5])

    def test_retry_after_is_capped_and_non_retryable_errors_are_not_retried(self):
        attempts = 0
        delays = []

        def capped_opener(request, *, timeout):
            nonlocal attempts
            del timeout
            attempts += 1
            if attempts == 1:
                raise HTTPError(request.full_url, 429, "busy", {"Retry-After": "120"}, None)
            return FakeResponse(self.token_response())

        NcentralClient(
            self.configuration,
            opener=capped_opener,
            sleeper=delays.append,
            jitter=lambda: 1.0,
        ).authenticate()
        self.assertEqual(attempts, 2)
        self.assertEqual(delays, [30.0])

        non_retry_attempts = 0

        def rejected(request, *, timeout):
            nonlocal non_retry_attempts
            del timeout
            non_retry_attempts += 1
            raise HTTPError(request.full_url, 400, "bad request", {}, None)

        with self.assertRaisesRegex(NcentralRequestError, "HTTP 400"):
            NcentralClient(
                self.configuration,
                opener=rejected,
                sleeper=lambda _delay: self.fail("HTTP 400 must not be retried"),
            ).authenticate()
        self.assertEqual(non_retry_attempts, 1)

    def test_authenticated_get_refreshes_access_token_once_after_401(self):
        auth_count = 0
        validate_authorizations = []

        def opener(request, *, timeout):
            nonlocal auth_count
            del timeout
            path = urlparse(request.full_url).path
            if path == "/api/auth/authenticate":
                auth_count += 1
                return FakeResponse(
                    {
                        "tokens": {
                            "access": {
                                "token": f"temporary-access-token-{auth_count}",
                                "expirySeconds": 3600,
                            }
                        }
                    }
                )
            if path == "/api/auth/validate":
                authorization = request.get_header("Authorization")
                validate_authorizations.append(authorization)
                if authorization == "Bearer temporary-access-token-1":
                    raise HTTPError(request.full_url, 401, "expired", {}, None)
                return FakeResponse({"message": "The token is valid."})
            if path == "/api/org-units":
                return FakeResponse({"data": [], "totalItems": 0})
            raise AssertionError(path)

        result = NcentralClient(self.configuration, opener=opener).test_connection()

        self.assertTrue(result["reachable"])
        self.assertEqual(auth_count, 2)
        self.assertEqual(
            validate_authorizations,
            [
                "Bearer temporary-access-token-1",
                "Bearer temporary-access-token-2",
            ],
        )

    def test_authenticated_get_does_not_refresh_more_than_once(self):
        auth_count = 0
        validate_count = 0

        def opener(request, *, timeout):
            nonlocal auth_count, validate_count
            del timeout
            path = urlparse(request.full_url).path
            if path == "/api/auth/authenticate":
                auth_count += 1
                return FakeResponse(
                    {
                        "tokens": {
                            "access": {
                                "token": f"temporary-access-token-{auth_count}",
                                "expirySeconds": 3600,
                            }
                        }
                    }
                )
            if path == "/api/auth/validate":
                validate_count += 1
                raise HTTPError(request.full_url, 401, "unauthorized", {}, None)
            raise AssertionError(path)

        with self.assertRaisesRegex(NcentralRequestError, "HTTP 401"):
            NcentralClient(self.configuration, opener=opener).test_connection()
        self.assertEqual(auth_count, 2)
        self.assertEqual(validate_count, 2)

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
