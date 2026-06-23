"""Artifact-storage provider configuration (chat media bytes).

Defines the operator-managed :class:`ArtifactStorageProvider` entity and
the discriminated set of backend configs (DB / filesystem / S3) it can
carry.
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from primer.model.common import Identifiable


class ArtifactStorageProviderType(str, Enum):
    """Supported artifact-storage backends for chat media bytes."""

    DB = "db"
    FILESYSTEM = "filesystem"
    S3 = "s3"


class DbArtifactConfig(BaseModel):
    """Config for the DB-backed artifact store (no fields).

    Bytes are persisted as ``Artifact`` rows through the platform
    StorageProvider; there is nothing backend-specific to configure.
    """

    model_config = ConfigDict(extra="forbid")


class FilesystemArtifactConfig(BaseModel):
    """Config for a filesystem-backed artifact store (v1 stub)."""

    model_config = ConfigDict(extra="forbid")

    root: str = Field(
        ...,
        description="Directory under which artifact bytes are written.",
    )


class S3ArtifactConfig(BaseModel):
    """Config for an S3 / S3-compatible artifact store (v1 stub)."""

    model_config = ConfigDict(extra="forbid")

    bucket: str = Field(..., description="Target bucket name.")
    prefix: str = Field(default="", description="Key prefix for stored objects.")
    endpoint_url: str | None = Field(
        default=None, description="Override endpoint (S3-compatible stores).",
    )
    region: str | None = Field(default=None, description="Bucket region.")
    access_key: SecretStr | None = Field(
        default=None, description="Access key id.",
    )
    secret_key: SecretStr | None = Field(
        default=None, description="Secret access key.",
    )


class ArtifactStorageProvider(Identifiable):
    """Operator-managed artifact-storage backend for chat media bytes.

    Stored as a CRUD-able row alongside the other providers. The
    discriminated ``config`` carries backend-specific settings; the
    ``provider`` discriminator chooses which config shape is valid. The
    default ``DB`` backend needs no config.
    """

    _id_prefix: ClassVar[str] = "artifact-storage-provider"

    provider: ArtifactStorageProviderType = Field(
        ...,
        description="Which artifact-storage backend to use.",
    )
    config: DbArtifactConfig | FilesystemArtifactConfig | S3ArtifactConfig = Field(
        default_factory=DbArtifactConfig,
        description="Backend-specific settings; must match ``provider``.",
    )

    @model_validator(mode="after")
    def _validate_config_matches(self) -> "ArtifactStorageProvider":
        expected = {
            ArtifactStorageProviderType.DB: DbArtifactConfig,
            ArtifactStorageProviderType.FILESYSTEM: FilesystemArtifactConfig,
            ArtifactStorageProviderType.S3: S3ArtifactConfig,
        }[self.provider]
        if not isinstance(self.config, expected):
            raise ValueError(
                f"provider={self.provider.value!r} requires a "
                f"{expected.__name__} in 'config'"
            )
        return self
