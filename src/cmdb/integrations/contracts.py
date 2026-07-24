"""Stable contracts shared by provider inputs, outputs and workflow actions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

OperationDirection = Literal["input", "output", "bidirectional", "trigger", "action"]
OperationStatus = Literal["available", "planned", "disabled"]


@dataclass(frozen=True)
class ProviderOperation:
    """Describe one bounded provider capability exposed to the platform."""

    key: str
    label: str
    direction: OperationDirection
    entity_type: str
    status: OperationStatus
    description: str
    requires_approval: bool = False
    writes_provider: bool = False


@dataclass(frozen=True)
class FilterDefinition:
    """Describe a safe discovery filter rendered by a setup wizard."""

    key: str
    label: str
    kind: Literal["multi_select", "boolean", "external_id_list"]
    description: str


@dataclass(frozen=True)
class ProviderManifest:
    """Declare provider identity, setup guidance and supported operations."""

    key: str
    name: str
    vendor: str
    version: str
    description: str
    scopes: tuple[Literal["msp", "customer"], ...]
    authentication_modes: tuple[str, ...]
    prerequisites: tuple[str, ...]
    operations: tuple[ProviderOperation, ...]
    filters: tuple[FilterDefinition, ...] = field(default_factory=tuple)
    documentation_path: str = ""

    def public_dict(self) -> dict[str, Any]:
        """Return a JSON-safe provider catalogue entry."""

        return asdict(self)


@dataclass(frozen=True)
class ConnectionTestStage:
    """Represent one sanitised stage of a progressive connection test."""

    key: str
    label: str
    status: Literal["passed", "failed", "skipped"]
    message: str


class IntegrationProvider(Protocol):
    """Define the minimum adapter surface used by the integration service."""

    manifest: ProviderManifest

    def test_connection(self, configuration: dict[str, Any]) -> dict[str, Any]:
        """Run bounded provider-specific connection tests."""

    def discover(self, configuration: dict[str, Any]) -> list[dict[str, Any]]:
        """Collect and normalize provider observations without canonical writes."""

    def apply_filters(
        self, observations: list[dict[str, Any]], policy: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return included observations and sanitised exclusion evidence."""

    def discovery_options(self, configuration: dict[str, Any]) -> dict[str, Any]:
        """Return bounded provider values used to populate discovery filters."""

    def preview(self, configuration: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
        """Return a bounded, read-only policy preview without persisting observations."""
