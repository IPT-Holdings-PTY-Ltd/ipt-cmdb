"""Capability-driven integration adapters and orchestration contracts."""

from .providers.connectwise import ConnectWiseProvider
from .providers.ncentral import NcentralProvider
from .registry import IntegrationProviderRegistry, provider_registry

provider_registry.register(ConnectWiseProvider())
provider_registry.register(NcentralProvider())

__all__ = ["IntegrationProviderRegistry", "provider_registry"]
