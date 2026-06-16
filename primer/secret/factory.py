"""Factory that dispatches a :class:`SecretProviderConfig` to a concrete provider."""

from __future__ import annotations

from primer.int.secret_provider import SecretProvider
from primer.model.except_ import ConfigError
from primer.model.provider import SecretProviderConfig, SecretProviderType


class SecretProviderFactory:
    """Construct a :class:`SecretProvider` from a discriminated config."""

    @staticmethod
    def create(config: SecretProviderConfig) -> SecretProvider:
        """Return an un-initialised provider matching ``config.provider``.

        Caller is responsible for ``await provider.initialize()`` before
        use and ``await provider.aclose()`` at shutdown.
        """
        if config.provider == SecretProviderType.ENV:
            from primer.secret.env import EnvSecretProvider

            return EnvSecretProvider(prefix=config.config.prefix)
        raise ConfigError(f"unknown SecretProviderType {config.provider!r}")
