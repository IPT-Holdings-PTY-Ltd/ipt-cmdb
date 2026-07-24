"""Provider registry used by APIs, setup wizards and future workers."""

from __future__ import annotations

from .contracts import IntegrationProvider


class IntegrationProviderRegistry:
    """Hold reviewed built-in adapters without executing arbitrary plugin code."""

    def __init__(self) -> None:
        self._providers: dict[str, IntegrationProvider] = {}

    def register(self, provider: IntegrationProvider) -> None:
        """Register one adapter and reject accidental key collisions."""

        key = provider.manifest.key
        if key in self._providers:
            raise ValueError(f"Integration provider already registered: {key}")
        self._providers[key] = provider

    def get(self, key: str) -> IntegrationProvider:
        """Return one provider or raise a stable lookup error."""

        try:
            return self._providers[key]
        except KeyError as error:
            raise ValueError("Integration provider not found") from error

    def manifests(self) -> list[dict]:
        """Return the public provider catalogue in stable display order."""

        return [
            provider.manifest.public_dict()
            for provider in sorted(self._providers.values(), key=lambda item: item.manifest.name)
        ]


provider_registry = IntegrationProviderRegistry()
