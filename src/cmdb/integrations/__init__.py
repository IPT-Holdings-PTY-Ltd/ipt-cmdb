"""Capability-driven integration adapters and orchestration contracts."""

from .providers.connectwise import ConnectWiseProvider
from .registry import IntegrationProviderRegistry, provider_registry

provider_registry.register(ConnectWiseProvider())

__all__ = ["IntegrationProviderRegistry", "provider_registry"]
