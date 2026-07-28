"""Bounded, read-only N-central REST API client and inventory normalizers."""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("cmdb.integrations.ncentral")
MAX_ASSET_ENRICHMENT_WORKERS = 4


class NcentralConfigurationError(ValueError):
    """Report invalid or incomplete N-central connection settings."""


class NcentralRequestError(RuntimeError):
    """Report a provider failure without exposing tokens or response bodies."""

    def __init__(self, message: str, *, status_code: int | None = None):
        """Retain a safe HTTP status for actionable server-side error mapping."""

        super().__init__(message)
        self.status_code = status_code


def normalize_base_url(value: str) -> str:
    """Return a safe HTTPS N-central server root without URL credentials."""

    candidate = str(value or "").strip().rstrip("/")
    if candidate.casefold().endswith("/api"):
        candidate = candidate[:-4]
    parsed = urlparse(candidate)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise NcentralConfigurationError(
            "N-central server URL must be HTTPS without embedded credentials, query or fragment"
        )
    return candidate


def normalize_org_unit(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize one N-central customer organization for explicit mapping."""

    external_id = str(record.get("orgUnitId") or "").strip()
    name = str(record.get("orgUnitName") or "").strip()
    if not external_id or not name:
        raise NcentralRequestError("N-central returned an organization without an ID or name")
    unit_type = str(record.get("orgUnitType") or "").strip().upper()
    return {
        "externalId": external_id[:160],
        "identifier": str(record.get("externalId") or record.get("externalId2") or "").strip()[
            :160
        ],
        "name": name[:240],
        "status": "Active",
        "type": unit_type or "CUSTOMER",
        "typeValues": [unit_type or "CUSTOMER"],
        "site": str(record.get("parentId") or "").strip()[:160],
        "deleted": False,
        "lastUpdated": "",
    }


def _asset_section(payload: dict[str, Any], *keys: str) -> Any:
    """Read a nested asset section across documented and legacy response shapes."""

    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def normalize_device(
    record: dict[str, Any], asset_payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Normalize a device into the provider-neutral reconciliation record."""

    external_id = str(record.get("deviceId") or "").strip()
    name = str(record.get("longName") or record.get("discoveredName") or "").strip()
    if not external_id or not name:
        raise NcentralRequestError("N-central returned a device without an ID or name")
    device_class = str(
        record.get("deviceClassLabel") or record.get("deviceClass") or "Device"
    ).strip()
    device_class_id = str(record.get("deviceClass") or device_class or "__unassigned__").strip()
    license_mode = str(record.get("licenseMode") or "Managed").strip()
    os_label = str(record.get("supportedOsLabel") or record.get("supportedOs") or "").strip()
    asset = asset_payload if isinstance(asset_payload, dict) else {}
    if isinstance(asset.get("data"), dict):
        asset = asset["data"]
    system = (
        _asset_section(asset, "computersystem") or _asset_section(asset, "computerSystem") or {}
    )
    network_rows = (
        _asset_section(asset, "networkadapter", "list")
        or _asset_section(asset, "networkAdapter", "list")
        or []
    )
    network = network_rows[0] if isinstance(network_rows, list) and network_rows else {}
    if not isinstance(network, dict):
        network = {}
    if not isinstance(system, dict):
        system = {}
    serial = str(
        system.get("serialnumber") or system.get("serialNumber") or record.get("serialNumber") or ""
    ).strip()
    model = str(system.get("model") or record.get("model") or "").strip()
    manufacturer = str(system.get("manufacturer") or "").strip()
    ip_address = str(
        network.get("ip")
        or network.get("ipaddress")
        or network.get("ipAddress")
        or network.get("IPAddress")
        or record.get("ipAddress")
        or ""
    ).strip()
    mac_address = str(
        network.get("mac")
        or network.get("macaddress")
        or network.get("macAddress")
        or network.get("MACAddress")
        or record.get("macAddress")
        or ""
    ).strip()
    fields = {
        "serialNumber": serial,
        "model": model,
        "manufacturer": manufacturer,
        "ipAddress": ip_address,
        "macAddress": mac_address,
        "operatingSystem": os_label,
        "lastLoggedInUser": str(record.get("lastLoggedInUser") or "").strip()[:240],
        "ncentralDeviceId": external_id,
        "ncentralDeviceClass": device_class,
        "ncentralLicenseMode": license_mode,
        "ncentralCustomerId": str(record.get("customerId") or "").strip()[:160],
        "ncentralSiteId": str(record.get("siteId") or "").strip()[:160],
        "ncentralSiteName": str(record.get("siteName") or "").strip()[:240],
        "lastAgentCheckIn": str(record.get("lastApplianceCheckinTime") or "").strip()[:80],
    }
    return {
        "externalId": external_id[:160],
        "name": name[:240],
        "type": device_class or "Device",
        "status": license_mode or "Managed",
        "providerTypeId": device_class_id[:160] or "__unassigned__",
        "providerTypeName": device_class[:160] or "Device",
        "providerStatusId": license_mode[:160] or "__unassigned__",
        "providerStatusName": license_mode[:160] or "Managed",
        "fields": {key: value for key, value in fields.items() if value not in ("", None)},
        "metadata": {
            "lifecycle": "in_service",
            "operationalStatus": "unknown",
            "site": fields["ncentralSiteName"],
            "ipAddress": ip_address,
            "model": model,
            "serialNumber": serial,
            "sourceSystem": "N-central",
        },
        "identifiers": {
            key: value
            for key, value in {
                "serial_number": serial,
                "mac_address": mac_address,
            }.items()
            if value
        },
        "providerVersion": fields["lastAgentCheckIn"],
    }


class NcentralClient:
    """Read N-central organizations and devices through short-lived access tokens."""

    def __init__(
        self,
        configuration: dict[str, Any],
        *,
        opener: Callable[..., Any] = urlopen,
        timeout: int = 20,
        telemetry_callback: Callable[[dict[str, Any]], Any] | None = None,
    ):
        self.base_url = normalize_base_url(str(configuration.get("baseUrl") or ""))
        self.user_api_token = str(configuration.get("userApiToken") or "").strip()
        if not self.user_api_token:
            raise NcentralConfigurationError("N-central connection is missing userApiToken")
        page_size = int(configuration.get("pageSize") or 250)
        if not 25 <= page_size <= 1000:
            raise NcentralConfigurationError("N-central page size must be between 25 and 1000")
        self.page_size = page_size
        self.timeout = max(5, min(int(timeout), 60))
        self.opener = opener
        self.telemetry_callback = telemetry_callback
        self._access_token = ""
        self._access_token_expires_at = 0.0
        self._last_success_telemetry_at = 0.0

    def _emit_telemetry(
        self, request: Request, status: int, retry_after: int | None = None
    ) -> None:
        """Emit only safe request-path and concurrency-throttle evidence."""

        if not self.telemetry_callback:
            return
        now = time.monotonic()
        if status < 400:
            if now - self._last_success_telemetry_at < 30:
                return
            self._last_success_telemetry_at = now
        try:
            self.telemetry_callback(
                {
                    "httpStatus": status,
                    "limit": None,
                    "remaining": None,
                    "resetAt": None,
                    "retryAfterSeconds": retry_after,
                    "limited": status == 429,
                    "requestPath": urlparse(request.full_url).path[:500],
                    "metadata": {"providerLimit": "in_flight_requests"},
                }
            )
        except Exception:
            LOGGER.exception("Could not persist sanitized N-central request telemetry")

    def _decode(self, response: Any, request: Request) -> Any:
        """Decode JSON and record provider status without exposing its body."""

        status = int(getattr(response, "status", None) or response.getcode() or 200)
        self._emit_telemetry(request, status)
        return json.loads(response.read())

    def _open_json(self, request: Request) -> Any:
        """Execute one bounded request with sanitized provider errors."""

        try:
            # The administrator-supplied endpoint is validated as HTTPS above.
            with self.opener(request, timeout=self.timeout) as response:  # nosec B310
                return self._decode(response, request)
        except HTTPError as error:
            retry_after = None
            with suppress(TypeError, ValueError):
                retry_after = int(str(error.headers.get("Retry-After") or "").strip())
            self._emit_telemetry(request, int(error.code), retry_after)
            raise NcentralRequestError(
                f"N-central returned HTTP {error.code}",
                status_code=int(error.code),
            ) from error
        except (URLError, TimeoutError) as error:
            raise NcentralRequestError("N-central could not be reached before timeout") from error
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
            raise NcentralRequestError("N-central returned an invalid JSON response") from error

    @staticmethod
    def _access_token_from(payload: Any) -> tuple[str, int]:
        """Extract the documented access token while tolerating wrapper differences."""

        if not isinstance(payload, dict):
            raise NcentralRequestError("N-central returned an unexpected authentication response")
        tokens = payload.get("tokens")
        access = tokens.get("access") if isinstance(tokens, dict) else None
        token = str(access.get("token") or "").strip() if isinstance(access, dict) else ""
        try:
            expiry = int(access.get("expirySeconds") or 3600) if isinstance(access, dict) else 3600
        except (TypeError, ValueError):
            expiry = 3600
        if not token:
            raise NcentralRequestError("N-central authentication did not return an access token")
        return token, max(60, min(expiry, 86400))

    def authenticate(self) -> dict[str, Any]:
        """Exchange the permanent User-API token without persisting the access token."""

        request = Request(
            f"{self.base_url}/api/auth/authenticate",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.user_api_token}",
                "User-Agent": "IPT-CMDB/1.0 (read-only N-central inventory)",
            },
            method="POST",
        )
        token, expiry = self._access_token_from(self._open_json(request))
        self._access_token = token
        self._access_token_expires_at = time.monotonic() + expiry - 30
        return {"authenticated": True, "expirySeconds": expiry}

    def _token(self) -> str:
        """Return a live in-memory access token, exchanging when necessary."""

        if not self._access_token or time.monotonic() >= self._access_token_expires_at:
            self.authenticate()
        return self._access_token

    def _get(self, path: str, parameters: dict[str, Any] | None = None) -> Any:
        """Read one hard-coded API path using a temporary access token."""

        query = urlencode(parameters or {}, doseq=True)
        url = f"{self.base_url}{path}{'?' + query if query else ''}"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token()}",
                "User-Agent": "IPT-CMDB/1.0 (read-only N-central inventory)",
            },
            method="GET",
        )
        return self._open_json(request)

    @staticmethod
    def _rows(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return collection rows and safe pagination metadata."""

        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise NcentralRequestError("N-central returned an unexpected collection response")
        rows = payload["data"]
        if any(not isinstance(item, dict) for item in rows):
            raise NcentralRequestError("N-central returned an invalid collection item")
        page_size = int(payload.get("pageSize") or len(rows) or 1)
        total_items = int(payload.get("totalItems") or len(rows))
        return rows, {
            "pageNumber": int(payload.get("pageNumber") or 1),
            "pageSize": page_size,
            "totalPages": int(
                payload.get("totalPages") or max(1, math.ceil(total_items / page_size))
            ),
            "totalItems": total_items,
        }

    def validate_access(self) -> dict[str, Any]:
        """Validate the short-lived token with the provider's read-only endpoint."""

        payload = self._get("/api/auth/validate")
        return (
            {"valid": True, "message": str(payload.get("message") or "")[:240]}
            if isinstance(payload, dict)
            else {"valid": True, "message": str(payload)[:240]}
        )

    def test_connection(self) -> dict[str, Any]:
        """Prove token exchange, validation and organization-read permission."""

        authentication = self.authenticate()
        self.validate_access()
        payload = self._get(
            "/api/org-units",
            {"pageNumber": 1, "pageSize": 1, "sortBy": "orgUnitId", "sortOrder": "asc"},
        )
        rows, metadata = self._rows(payload)
        return {
            "reachable": True,
            "accessExpirySeconds": authentication["expirySeconds"],
            "sampleOrganization": normalize_org_unit(rows[0]) if rows else None,
            "accessibleOrganizations": metadata["totalItems"],
        }

    def _paged(
        self,
        path: str,
        parameters: dict[str, Any] | None = None,
        *,
        max_pages: int = 100,
    ) -> list[dict[str, Any]]:
        """Read a bounded collection using documented one-based pagination."""

        discovered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, max(1, min(int(max_pages), 100)) + 1):
            payload = self._get(
                path,
                {
                    **(parameters or {}),
                    "pageNumber": page,
                    "pageSize": self.page_size,
                },
            )
            rows, metadata = self._rows(payload)
            for row in rows:
                key = json.dumps(row, sort_keys=True, separators=(",", ":"))
                if key not in seen:
                    seen.add(key)
                    discovered.append(row)
            if (
                page >= metadata["totalPages"]
                or len(discovered) >= metadata["totalItems"]
                or not rows
            ):
                return discovered
        raise NcentralRequestError(
            f"N-central collection exceeded the {max_pages}-page safety limit"
        )

    def discover_customers(self) -> list[dict[str, Any]]:
        """Return accessible CUSTOMER organization units for explicit CMDB mapping."""

        rows = self._paged(
            "/api/org-units",
            {
                "sortBy": "orgUnitId",
                "sortOrder": "asc",
            },
        )
        return [
            normalize_org_unit(row)
            for row in rows
            if str(row.get("orgUnitType") or "").upper() == "CUSTOMER"
        ]

    def list_device_filters(self) -> list[dict[str, str]]:
        """Return immutable filter IDs available to the configured N-central user."""

        rows = self._paged(
            "/api/device-filters",
            {"viewScope": "ALL", "sortBy": "filterName", "sortOrder": "asc"},
        )
        return sorted(
            [
                {
                    "id": str(row.get("filterId") or "").strip()[:160],
                    "name": str(row.get("filterName") or "").strip()[:240],
                    "description": str(row.get("description") or "").strip()[:500],
                }
                for row in rows
                if str(row.get("filterId") or "").strip()
                and str(row.get("filterName") or "").strip()
            ],
            key=lambda item: (item["name"].casefold(), item["id"]),
        )

    def discover_devices(
        self,
        org_unit_id: str,
        *,
        filter_id: str = "",
        enrich_limit: int = 0,
    ) -> list[dict[str, Any]]:
        """Return devices for one explicitly mapped organization unit."""

        parent_id = str(org_unit_id or "").strip()
        if not parent_id or not parent_id.replace("-", "").isalnum():
            raise NcentralConfigurationError("Choose a valid N-central organization ID")
        parameters: dict[str, Any] = {
            "sortBy": "deviceId",
            "sortOrder": "asc",
        }
        selected_filter = str(filter_id or "").strip()
        if selected_filter:
            if not selected_filter.replace("-", "").replace("_", "").isalnum():
                raise NcentralConfigurationError("Choose a valid N-central device filter ID")
            parameters["filterId"] = selected_filter
        rows = self._paged(f"/api/org-units/{parent_id}/devices", parameters)
        bounded_enrichment = max(0, min(int(enrich_limit), 50))
        enrichment_targets = [
            (index, str(row.get("deviceId") or ""))
            for index, row in enumerate(rows[:bounded_enrichment])
            if str(row.get("deviceId") or "").isdigit()
        ]
        asset_payloads: dict[int, dict[str, Any]] = {}
        if enrichment_targets:
            worker_count = min(MAX_ASSET_ENRICHMENT_WORKERS, len(enrichment_targets))
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="ncentral-asset-read",
            ) as executor:
                pending = {
                    executor.submit(self._get, f"/api/devices/{int(device_id)}/assets"): index
                    for index, device_id in enrichment_targets
                }
                for future in as_completed(pending):
                    index = pending[future]
                    payload = future.result()
                    if isinstance(payload, dict):
                        asset_payloads[index] = payload

        records: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            records.append(normalize_device(row, asset_payloads.get(index)))
        return records
