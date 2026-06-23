"""Secret provider configuration.

Defines the top-level :class:`SecretProviderConfig` and its env-backed
config. A secret named ``foo`` resolves to an environment variable under
the configured prefix.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class SecretProviderType(str, Enum):
    """Supported Secret provider backends."""

    ENV = "env"


class EnvSecretConfig(BaseModel):
    """Settings for the env-backed Secret provider.

    A secret named ``foo`` resolves to the environment variable
    ``<prefix><FOO>`` (name upper-cased). With the default prefix,
    ``foo`` -> ``PRIMER_SECRET_FOO``.
    """

    prefix: str = Field(
        default="PRIMER_SECRET_",
        description=(
            "Environment-variable prefix. The secret name is "
            "upper-cased and appended to this prefix."
        ),
    )


class SecretProviderConfig(BaseModel):
    """Top-level Secret provider configuration -- discriminated by ``provider``."""

    provider: SecretProviderType = Field(
        default=SecretProviderType.ENV,
        description="Which Secret backend to use.",
    )
    config: EnvSecretConfig = Field(
        default_factory=EnvSecretConfig,
        description="Backend-specific settings; must match ``provider``.",
    )

    @model_validator(mode="after")
    def _validate_config_matches(self) -> "SecretProviderConfig":
        if self.provider == SecretProviderType.ENV and not isinstance(
            self.config, EnvSecretConfig
        ):
            raise ValueError(
                "provider='env' requires an EnvSecretConfig in 'config'"
            )
        return self
