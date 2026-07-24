"""ConnectWise PSA adapter implementing the shared provider contract."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

from src.cmdb.connectwise import ConnectWiseClient

from ..contracts import (
    ConnectionTestStage,
    FilterDefinition,
    ProviderManifest,
    ProviderOperation,
)

CONNECTWISE_MANIFEST = ProviderManifest(
    key="connectwise",
    name="ConnectWise PSA",
    vendor="ConnectWise",
    version="1.0",
    description=(
        "Read company observations into a review-gated mapping pipeline. "
        "Provider writes are declared for future workflows but remain disabled."
    ),
    scopes=("msp", "customer"),
    authentication_modes=("api_member_keys", "environment_managed"),
    prerequisites=(
        "Dedicated API member",
        "Company read permission",
        "Regional REST API base URL",
        "ConnectWise client ID",
    ),
    operations=(
        ProviderOperation(
            key="company.discover",
            label="Discover companies",
            direction="input",
            entity_type="company",
            status="available",
            description="Read and normalize company metadata for explicit customer mapping.",
        ),
        ProviderOperation(
            key="configuration_item.discover",
            label="Discover configuration items",
            direction="input",
            entity_type="configuration_item",
            status="available",
            description=(
                "Preview and selectively import mapped-customer configuration items "
                "through provider-ID-first reconciliation."
            ),
        ),
        ProviderOperation(
            key="change_ticket.create",
            label="Create change ticket",
            direction="action",
            entity_type="change_request",
            status="disabled",
            description="Future approval-gated ticket publishing workflow action.",
            requires_approval=True,
            writes_provider=True,
        ),
    ),
    filters=(
        FilterDefinition(
            key="includedStatuses",
            label="Company statuses",
            kind="multi_select",
            description="Include only selected ConnectWise company statuses.",
        ),
        FilterDefinition(
            key="includedTypes",
            label="Company types",
            kind="multi_select",
            description="Include only selected ConnectWise company types.",
        ),
        FilterDefinition(
            key="includedSites",
            label="Territories",
            kind="multi_select",
            description="Include only selected ConnectWise company territories.",
        ),
        FilterDefinition(
            key="includeDeleted",
            label="Include deleted companies",
            kind="boolean",
            description="Deleted provider companies are excluded by default.",
        ),
        FilterDefinition(
            key="excludedExternalIds",
            label="Explicit exclusions",
            kind="external_id_list",
            description="Never include these immutable provider company IDs.",
        ),
    ),
    documentation_path="/docs/CONNECTWISE.md",
)


def normalized_policy(value: dict[str, Any] | None) -> dict[str, Any]:
    """Return a bounded, deterministic ConnectWise discovery policy."""

    raw = value or {}

    def values(key: str) -> list[str]:
        items = raw.get(key) or []
        if not isinstance(items, list):
            return []
        return sorted({str(item).strip()[:160] for item in items if str(item).strip()})[:500]

    return {
        "includedStatuses": values("includedStatuses"),
        "includedTypes": values("includedTypes"),
        "includedSites": values("includedSites"),
        "includeDeleted": bool(raw.get("includeDeleted", False)),
        "excludedExternalIds": values("excludedExternalIds"),
    }


class ConnectWiseProvider:
    """Expose ConnectWise reads through the common adapter boundary."""

    manifest = CONNECTWISE_MANIFEST

    def __init__(self, client_factory: Callable[[dict[str, Any]], Any] = ConnectWiseClient):
        self.client_factory = client_factory

    def test_connection(self, configuration: dict[str, Any]) -> dict[str, Any]:
        """Test configuration, authentication and company-read permission in order."""

        client = self.client_factory(configuration)
        result = client.test_connection()
        stages = [
            ConnectionTestStage(
                "configuration",
                "Configuration",
                "passed",
                "Required connection values and HTTPS API root are valid.",
            ),
            ConnectionTestStage(
                "authentication",
                "Authentication",
                "passed",
                "ConnectWise accepted the API member credentials.",
            ),
            ConnectionTestStage(
                "company_read",
                "Company read permission",
                "passed",
                "A bounded company read completed successfully.",
            ),
        ]
        return {
            **result,
            "stages": [stage.__dict__ for stage in stages],
            "writesAttempted": False,
        }

    def discover(self, configuration: dict[str, Any]) -> list[dict[str, Any]]:
        """Collect normalized company observations through the bounded client."""

        return self.client_factory(configuration).discover_companies()

    def _preview_observations(
        self, configuration: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], bool]:
        """Read one large page so setup screens never wait for full pagination."""

        client = self.client_factory(configuration)
        preview_method = getattr(client, "preview_companies", None)
        if callable(preview_method):
            return preview_method(limit=1000)
        # Preserve compatibility with reviewed adapters used by older tests/extensions.
        return client.discover_companies(), False

    @staticmethod
    def _observation_types(observation: dict[str, Any]) -> list[str]:
        """Return individual company types from current and legacy normalized records."""

        values = observation.get("typeValues")
        if isinstance(values, list):
            return [str(item) for item in values if str(item).strip()]
        value = str(observation.get("type") or "").strip()
        return [value] if value else []

    @staticmethod
    def _available_values(observations: list[dict[str, Any]]) -> dict[str, list[str]]:
        """Return sorted non-empty filter values from normalized observations."""

        return {
            "availableStatuses": sorted(
                {str(item.get("status") or "") for item in observations if item.get("status")}
            ),
            "availableTypes": sorted(
                {
                    value
                    for item in observations
                    for value in ConnectWiseProvider._observation_types(item)
                }
            ),
            "availableSites": sorted(
                {str(item.get("site") or "") for item in observations if item.get("site")}
            ),
        }

    def _territory_values(
        self, configuration: dict[str, Any], observations: list[dict[str, Any]]
    ) -> list[str]:
        """Prefer the complete provider catalogue, with a legacy adapter fallback."""

        client = self.client_factory(configuration)
        territory_method = getattr(client, "list_territories", None)
        if callable(territory_method):
            return territory_method(limit=1000)
        return self._available_values(observations)["availableSites"]

    def discovery_options(self, configuration: dict[str, Any]) -> dict[str, Any]:
        """Return responsive, bounded values for the wizard filter menus."""

        observations, truncated = self._preview_observations(configuration)
        available = self._available_values(observations)
        available["availableSites"] = self._territory_values(configuration, observations)
        return {
            **available,
            "sampled": len(observations),
            "truncated": truncated,
            "readOnly": True,
            "writesAttempted": False,
        }

    def apply_filters(
        self, observations: list[dict[str, Any]], policy: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Apply saved filters after normalization and retain exclusion reasons."""

        normalized = normalized_policy(policy)
        include_statuses = {item.casefold() for item in normalized["includedStatuses"]}
        include_types = {item.casefold() for item in normalized["includedTypes"]}
        include_territories = {item.casefold() for item in normalized["includedSites"]}
        excluded_ids = set(normalized["excludedExternalIds"])
        included: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for observation in observations:
            reason = ""
            if observation["externalId"] in excluded_ids:
                reason = "Explicit provider ID exclusion"
            elif observation.get("deleted") and not normalized["includeDeleted"]:
                reason = "Deleted in provider"
            elif (
                include_statuses
                and str(observation.get("status") or "").casefold() not in include_statuses
            ):
                reason = "Status filter"
            elif include_types and not {
                item.casefold() for item in self._observation_types(observation)
            }.intersection(include_types):
                reason = "Type filter"
            elif (
                include_territories
                and str(observation.get("site") or "").casefold() not in include_territories
            ):
                reason = "Territory filter"
            if reason:
                excluded.append(
                    {
                        "externalId": observation["externalId"],
                        "name": observation["name"],
                        "reason": reason,
                    }
                )
            else:
                included.append(observation)
        return included, excluded

    def preview(self, configuration: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
        """Return bounded dry-run counts, values and exclusion evidence."""

        observations, truncated = self._preview_observations(configuration)
        applied_policy = normalized_policy(policy)
        included, excluded = self.apply_filters(observations, applied_policy)
        reasons = Counter(item["reason"] for item in excluded)
        available = self._available_values(observations)
        available["availableSites"] = self._territory_values(configuration, observations)
        return {
            "readOnly": True,
            "writesAttempted": False,
            "appliedPolicy": applied_policy,
            "discovered": len(observations),
            "included": len(included),
            "excluded": len(excluded),
            "truncated": truncated,
            "exclusionReasons": dict(sorted(reasons.items())),
            **available,
            "sampleIncluded": included[:20],
            "sampleExcluded": excluded[:20],
        }
