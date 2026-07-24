"""Bounded, sanitized ConnectWise PSA company and configuration discovery."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


class ConnectWiseConfigurationError(ValueError):
    """Report invalid or incomplete ConnectWise connection settings."""


class ConnectWiseRequestError(RuntimeError):
    """Report a provider failure without exposing credentials or response bodies."""


def normalize_base_url(value: str) -> str:
    """Return a safe HTTPS API root without query, fragment or embedded credentials."""

    candidate = str(value or "").strip().rstrip("/")
    parsed = urlparse(candidate)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConnectWiseConfigurationError(
            "ConnectWise API URL must be HTTPS without embedded credentials, query or fragment"
        )
    return candidate


def normalize_company(record: dict[str, Any]) -> dict[str, Any]:
    """Keep only company fields needed for customer mapping and operator review."""

    external_id = str(record.get("id") or "").strip()
    name = str(record.get("name") or "").strip()
    if not external_id or not name:
        raise ConnectWiseRequestError("ConnectWise returned a company without an ID or name")

    def nested_name(key: str) -> str:
        value = record.get(key)
        return str(value.get("name") or "").strip() if isinstance(value, dict) else ""

    def nested_names(key: str) -> list[str]:
        value = record.get(key)
        if not isinstance(value, list):
            return []
        names = [
            str(item.get("name") or "").strip()[:160]
            for item in value
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        return list(dict.fromkeys(names))[:50]

    type_values = nested_names("types")
    if not type_values and nested_name("type"):
        type_values = [nested_name("type")[:160]]

    return {
        "externalId": external_id[:160],
        "identifier": str(record.get("identifier") or "").strip()[:160],
        "name": name[:240],
        "status": nested_name("status")[:160],
        "type": ", ".join(type_values)[:160],
        "typeValues": type_values,
        # ``site`` is the legacy CMDB storage key for this provider attribute.
        # Only ConnectWise company territory is accepted; a company's default
        # site is a separate concept and must not leak into the territory filter.
        "site": nested_name("territory")[:160],
        "deleted": bool(record.get("deletedFlag", False)),
        "lastUpdated": str(record.get("lastUpdated") or "").strip()[:80],
    }


def normalize_configuration(record: dict[str, Any]) -> dict[str, Any]:
    """Keep the bounded ConnectWise configuration fields used by CI reconciliation."""

    external_id = str(record.get("id") or "").strip()
    name = str(record.get("name") or "").strip()
    if not external_id or not name:
        raise ConnectWiseRequestError(
            "ConnectWise returned a configuration item without an ID or name"
        )

    def text(key: str, limit: int = 500) -> str:
        return str(record.get(key) or "").strip()[:limit]

    def reference(key: str) -> str:
        value = record.get(key)
        return str(value.get("name") or "").strip()[:160] if isinstance(value, dict) else ""

    def reference_id(key: str) -> str:
        value = record.get(key)
        return str(value.get("id") or "").strip()[:160] if isinstance(value, dict) else ""

    raw_provider_info = record.get("_info")
    provider_info: dict[str, Any] = raw_provider_info if isinstance(raw_provider_info, dict) else {}
    serial_number = text("serialNumber", 240)
    device_identifier = text("deviceIdentifier", 240)
    mobile_guid = text("mobileGuid", 240)
    provider_type_id = reference_id("type")
    provider_type_name = reference("type")
    provider_status_id = reference_id("status")
    provider_status_name = reference("status")
    model = text("modelNumber", 240)
    operating_system = text("osInfo", 500) or text("osType", 240)
    fields = {
        "serialNumber": serial_number,
        "model": model,
        "tagNumber": text("tagNumber", 160),
        "deviceIdentifier": device_identifier,
        "mobileGuid": mobile_guid,
        "ipAddress": text("ipAddress", 160),
        "macAddress": text("macAddress", 160),
        "defaultGateway": text("defaultGateway", 160),
        "operatingSystem": operating_system,
        "ram": text("ram", 160),
        "cpuSpeed": text("cpuSpeed", 160),
        "localHardDrives": text("localHardDrives", 500),
        "site": reference("site"),
        "location": reference("location"),
        "department": reference("department"),
        "connectWiseTypeId": provider_type_id,
        "connectWiseType": provider_type_name,
        "connectWiseStatusId": provider_status_id,
        "connectWiseStatus": provider_status_name,
        "managementLink": text("managementLink", 500),
        "remoteLink": text("remoteLink", 500),
        "active": bool(record.get("activeFlag", True)),
        "needsRenewal": bool(record.get("needsRenewalFlag", False)),
    }
    return {
        "externalId": external_id[:160],
        "name": name[:240],
        "type": provider_type_name or "Configuration item",
        "status": "Active" if fields["active"] else "Retired",
        "providerTypeId": provider_type_id or "__unassigned__",
        "providerTypeName": provider_type_name or "Unclassified",
        "providerStatusId": provider_status_id or "__unassigned__",
        "providerStatusName": provider_status_name or "Unassigned",
        "fields": {key: value for key, value in fields.items() if value not in ("", None)},
        "metadata": {
            "lifecycle": "in_service" if fields["active"] else "retired",
            "operationalStatus": "unknown",
            "site": fields["site"],
            "ipAddress": fields["ipAddress"],
            "model": model,
            "serialNumber": serial_number,
            "sourceSystem": "ConnectWise",
        },
        "identifiers": {
            key: value
            for key, value in {
                "serial_number": serial_number,
                "device_uuid": mobile_guid or device_identifier,
            }.items()
            if value
        },
        "providerVersion": str(provider_info.get("lastUpdated") or "").strip()[:80],
    }


class ConnectWiseClient:
    """Fetch ConnectWise inventory through bounded read-only requests."""

    def __init__(
        self,
        configuration: dict[str, Any],
        *,
        opener: Callable[..., Any] = urlopen,
        timeout: int = 20,
    ):
        self.base_url = normalize_base_url(str(configuration.get("baseUrl") or ""))
        required = {
            "companyId": str(configuration.get("companyId") or "").strip(),
            "publicKey": str(configuration.get("publicKey") or "").strip(),
            "privateKey": str(configuration.get("privateKey") or "").strip(),
            "clientId": str(configuration.get("clientId") or "").strip(),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ConnectWiseConfigurationError(
                "ConnectWise connection is missing " + ", ".join(missing)
            )
        page_size = int(configuration.get("pageSize") or 100)
        if not 25 <= page_size <= 1000:
            raise ConnectWiseConfigurationError("ConnectWise page size must be between 25 and 1000")
        raw = f"{required['companyId']}+{required['publicKey']}:{required['privateKey']}"
        self.headers = {
            "Accept": "application/json",
            "Authorization": "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii"),
            "clientId": required["clientId"],
            "User-Agent": "IPT-CMDB/1.0 (read-only company discovery)",
        }
        self.page_size = page_size
        self.timeout = max(5, min(int(timeout), 60))
        self.opener = opener

    def _collection(self, path: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        """Fetch one hard-coded API collection with sanitized provider errors."""

        query = urlencode(parameters)
        request = Request(
            f"{self.base_url}{path}?{query}",
            headers=self.headers,
            method="GET",
        )
        try:
            # The URL is validated as an administrator-supplied HTTPS endpoint above.
            with self.opener(request, timeout=self.timeout) as response:  # nosec B310
                payload = json.loads(response.read())
        except HTTPError as error:
            raise ConnectWiseRequestError(f"ConnectWise returned HTTP {error.code}") from error
        except (URLError, TimeoutError) as error:
            raise ConnectWiseRequestError(
                "ConnectWise could not be reached before timeout"
            ) from error
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
            raise ConnectWiseRequestError(
                "ConnectWise returned an invalid JSON response"
            ) from error
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise ConnectWiseRequestError("ConnectWise returned an unexpected collection response")
        return payload

    def _page(
        self, page: int, page_size: int | None = None, *, fields: str = ""
    ) -> list[dict[str, Any]]:
        """Fetch one company page in stable provider-ID order."""

        parameters: dict[str, Any] = {
            "page": page,
            "pageSize": page_size or self.page_size,
            "orderBy": "id",
        }
        if fields:
            parameters["fields"] = fields
        return self._collection("/company/companies", parameters)

    def list_territories(self, limit: int = 1000) -> list[str]:
        """Return all territory references used by accessible companies."""

        page_size = max(1, min(int(limit), 1000))
        territories: set[str] = set()
        for page in range(1, 101):
            rows = self._collection(
                "/company/companies",
                {
                    "page": page,
                    "pageSize": page_size,
                    "orderBy": "id",
                    "fields": "territory",
                },
            )
            for item in rows:
                territory = item.get("territory")
                if isinstance(territory, dict):
                    name = str(territory.get("name") or "").strip()[:160]
                    if name:
                        territories.add(name)
            if len(rows) < page_size:
                break
        return sorted(territories)

    def test_connection(self) -> dict[str, Any]:
        """Prove credentials and company-read permission using a one-record request."""

        records = self._page(1, 1)
        return {
            "reachable": True,
            "sampleCompany": normalize_company(records[0]) if records else None,
        }

    def preview_companies(self, *, limit: int = 1000) -> tuple[list[dict[str, Any]], bool]:
        """Return one bounded page for responsive filter choices and dry-run previews."""

        page_size = max(25, min(int(limit), 1000))
        payload = self._page(1, page_size)
        companies: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in payload:
            company = normalize_company(raw)
            if company["externalId"] in seen:
                continue
            seen.add(company["externalId"])
            companies.append(company)
        return companies, len(payload) >= page_size

    def discover_companies(
        self, *, max_pages: int = 100, page_size: int = 1000
    ) -> list[dict[str, Any]]:
        """Return all accessible companies using large, least-data pages."""

        bounded_pages = max(1, min(int(max_pages), 100))
        optimized_page_size = max(25, min(int(page_size), 1000))
        discovered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, bounded_pages + 1):
            payload = self._page(
                page,
                optimized_page_size,
                fields="id,identifier,name,status,types,territory,deletedFlag,lastUpdated",
            )
            for raw in payload:
                company = normalize_company(raw)
                if company["externalId"] in seen:
                    continue
                seen.add(company["externalId"])
                discovered.append(company)
            if len(payload) < optimized_page_size:
                return discovered
        raise ConnectWiseRequestError(
            f"ConnectWise company discovery exceeded the {bounded_pages}-page safety limit"
        )

    def discover_configurations(
        self, company_external_id: str, *, max_pages: int = 100, page_size: int = 1000
    ) -> list[dict[str, Any]]:
        """Return normalized configurations for one explicitly mapped company."""

        company_id = str(company_external_id or "").strip()
        if not company_id.isdigit():
            raise ConnectWiseConfigurationError(
                "ConnectWise company ID must be numeric for configuration discovery"
            )
        bounded_pages = max(1, min(int(max_pages), 100))
        bounded_page_size = max(25, min(int(page_size), 1000))
        discovered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, bounded_pages + 1):
            payload = self._collection(
                "/company/configurations",
                {
                    "conditions": f"company/id={company_id}",
                    "page": page,
                    "pageSize": bounded_page_size,
                    "orderBy": "id",
                },
            )
            for raw in payload:
                item = normalize_configuration(raw)
                if item["externalId"] in seen:
                    continue
                seen.add(item["externalId"])
                discovered.append(item)
            if len(payload) < bounded_page_size:
                return discovered
        raise ConnectWiseRequestError(
            f"ConnectWise configuration discovery exceeded the {bounded_pages}-page safety limit"
        )
