"""N-central adapter implementing the shared provider contract."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.cmdb.ncentral import NcentralClient

from ..contracts import ConnectionTestStage, ProviderManifest, ProviderOperation

NCENTRAL_MANIFEST = ProviderManifest(
    key="ncentral",
    name="N-central",
    vendor="N-able",
    version="1.2",
    description=(
        "Read customer organization and device inventory into provider-ID-first, "
        "review-gated CMDB reconciliation, with optional N-able GraphQL enrichment."
    ),
    scopes=("msp",),
    authentication_modes=(
        "user_api_token",
        "nable_graphql_api_token",
        "environment_managed",
        "docker_secret_file",
    ),
    prerequisites=(
        "Dedicated N-central API user with required access groups",
        "MFA disabled for the API user as required by N-central",
        "Generated N-central User-API token",
        "HTTPS N-central server URL",
        "Optional N-able Platform API token for read-only GraphQL enrichment",
    ),
    operations=(
        ProviderOperation(
            key="organization.discover",
            label="Discover customers",
            direction="input",
            entity_type="company",
            status="available",
            description="Read CUSTOMER organization units for explicit tenant mapping.",
        ),
        ProviderOperation(
            key="device.discover",
            label="Discover devices",
            direction="input",
            entity_type="configuration_item",
            status="available",
            description="Read mapped-customer devices through saved filters and reviewed imports.",
        ),
        ProviderOperation(
            key="asset.enrich",
            label="Enrich device inventory",
            direction="input",
            entity_type="configuration_item",
            status="available",
            description=(
                "Read explicitly mapped Customer assets through static N-able GraphQL queries."
            ),
        ),
        ProviderOperation(
            key="patch.observe",
            label="Read patch evidence",
            direction="input",
            entity_type="patch_installation",
            status="available",
            description=(
                "Read Customer-scoped patch installation evidence through a separate "
                "static query; patch data is never merged into base asset inventory."
            ),
        ),
        ProviderOperation(
            key="device.manage",
            label="Manage devices",
            direction="action",
            entity_type="configuration_item",
            status="disabled",
            description="Provider writes are intentionally outside this read-only connector.",
            requires_approval=True,
            writes_provider=True,
        ),
    ),
    documentation_path="/docs/NCENTRAL.md",
)


class NcentralProvider:
    """Expose N-central reads through the common adapter boundary."""

    manifest = NCENTRAL_MANIFEST

    def __init__(self, client_factory: Callable[[dict[str, Any]], Any] = NcentralClient):
        self.client_factory = client_factory

    def test_connection(self, configuration: dict[str, Any]) -> dict[str, Any]:
        """Test token exchange, token validation and least-privilege organization read."""

        result = self.client_factory(configuration).test_connection()
        stages = (
            ConnectionTestStage(
                "configuration",
                "Configuration",
                "passed",
                "The HTTPS server URL and connection settings are valid.",
            ),
            ConnectionTestStage(
                "token_exchange",
                "User-API token exchange",
                "passed",
                "N-central returned a short-lived access token.",
            ),
            ConnectionTestStage(
                "token_validation",
                "Access-token validation",
                "passed",
                "N-central accepted the temporary access token.",
            ),
            ConnectionTestStage(
                "organization_read",
                "Organization read permission",
                "passed",
                "A bounded organization-unit read completed successfully.",
            ),
        )
        return {
            **result,
            "stages": [stage.__dict__ for stage in stages],
            "writesAttempted": False,
        }

    def discover(self, configuration: dict[str, Any]) -> list[dict[str, Any]]:
        """Collect normalized CUSTOMER organizations."""

        return self.client_factory(configuration).discover_customers()

    def apply_filters(
        self, observations: list[dict[str, Any]], policy: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Retain CUSTOMER organizations and explicit immutable-ID exclusions."""

        excluded_ids = {
            str(value).strip()
            for value in policy.get("excludedExternalIds", [])
            if str(value).strip()
        }
        included: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for item in observations:
            reason = ""
            if item["externalId"] in excluded_ids:
                reason = "Explicit provider ID exclusion"
            elif str(item.get("type") or "").upper() != "CUSTOMER":
                reason = "Not a customer organization"
            if reason:
                excluded.append(
                    {"externalId": item["externalId"], "name": item["name"], "reason": reason}
                )
            else:
                included.append(item)
        return included, excluded

    def discovery_options(self, configuration: dict[str, Any]) -> dict[str, Any]:
        """Return accessible device filters for repeatable discovery policy choices."""

        filters = self.client_factory(configuration).list_device_filters()
        return {
            "deviceFilters": filters,
            "readOnly": True,
            "writesAttempted": False,
        }

    def preview(self, configuration: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
        """Return a read-only organization preview."""

        observations = self.discover(configuration)
        included, excluded = self.apply_filters(observations, policy)
        return {
            "readOnly": True,
            "writesAttempted": False,
            "appliedPolicy": {
                "excludedExternalIds": sorted(
                    {str(value) for value in policy.get("excludedExternalIds", []) if str(value)}
                )
            },
            "discovered": len(observations),
            "included": len(included),
            "excluded": len(excluded),
            "truncated": False,
            "sampleIncluded": included[:20],
            "sampleExcluded": excluded[:20],
        }
