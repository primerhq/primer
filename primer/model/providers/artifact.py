"""Artifact-storage provider configuration (chat media bytes).

Defines the operator-managed :class:`ArtifactStorageProvider` entity and
the discriminated set of backend configs (DB / filesystem / S3) it can
carry, and :class:`ActiveArtifactStorageConfig`, the singleton that names
the provider NEW artifacts are written to.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from primer.model.common import Identifiable

#: Reserved id of the auto-seeded default provider (the DB backend), so chat media works with zero operator configuration. It is the store
#: new artifacts go to until another is selected, and the store that holds every artifact with no location record. It is defined here, in a
#: leaf module, because ``primer.artifact`` must not import ``primer.api`` (``primer/artifact/checks.py`` reads it, ``primer.toolset.system``
#: imports that check, and ``primer.api`` imports ``primer.toolset.system``). ``primer.artifact.factory`` and
#: ``primer.api.registries.artifact_storage_registry`` re-export the same object.
DEFAULT_ARTIFACT_PROVIDER_ID: str = "artifact-storage-default"

#: Id of the singleton :class:`ActiveArtifactStorageConfig` row.
ACTIVE_ARTIFACT_STORAGE_CONFIG_ID: str = "_active_artifact_storage_config"

#: How an S3 request names its bucket: ``path`` puts it in the URL path, ``virtual`` in the host name, and ``auto`` lets primer choose
#: (``path`` when ``endpoint_url`` is set, which is what MinIO and most S3-compatible servers need, and botocore's own default on AWS).
S3AddressingStyle = Literal["auto", "path", "virtual"]


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
    """Config for a filesystem-backed artifact store (``primer.artifact.filesystem``): the artifact files live in one folder that every server
    process reads and writes."""

    model_config = ConfigDict(extra="forbid")

    root: str = Field(
        ...,
        description=(
            "Absolute path of the folder the artifact files are written under. It must be on a volume shared by every API and worker process, "
            "mounted at this same path in each of them."
        ),
    )


class S3ArtifactConfig(BaseModel):
    """Config for an S3 / S3-compatible artifact store.

    The four optional connection fields (``endpoint_url``, ``region``, ``access_key``, ``secret_key``) read a blank string as unset. The console
    sends ``""`` for an empty box (board ticket T3), and a ``""`` endpoint would otherwise reach botocore as an empty URL and, being another
    "origin" than the stored ``None``, make an edit refuse the served key masks. With no keys the store uses the ambient AWS credentials.
    """

    model_config = ConfigDict(extra="forbid")

    bucket: str = Field(..., description="Target bucket name.")
    prefix: str = Field(default="", description="Key prefix for stored objects.")
    endpoint_url: str | None = Field(
        default=None, description="Override endpoint (S3-compatible stores).",
    )
    region: str | None = Field(default=None, description="Bucket region.")
    addressing_style: S3AddressingStyle = Field(
        default="auto",
        description=(
            "How requests name the bucket: 'path', 'virtual' (virtual-hosted), or 'auto' (path-style when an "
            "endpoint URL is set, else botocore's own choice)."
        ),
    )
    access_key: SecretStr | None = Field(
        default=None, description="Access key id.",
    )
    secret_key: SecretStr | None = Field(
        default=None, description="Secret access key.",
    )

    @field_validator("endpoint_url", "region", "access_key", "secret_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        """A string that is empty once stripped is the unset value. ``prefix`` is not in the list: its empty string is a real value."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


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


class ActiveArtifactStorageConfig(Identifiable):
    """Singleton row at id :data:`ACTIVE_ARTIFACT_STORAGE_CONFIG_ID`: the provider NEW artifacts are written to.

    Read on every write and never cached, so a switch is seen by every process at once. Artifacts already written stay on the provider that
    holds them (see :class:`primer.model.artifact.ArtifactLocation`). The row is absent until an operator selects a store, which reads as the
    reserved default (:data:`DEFAULT_ARTIFACT_PROVIDER_ID`, the database). It has no id prefix: the row is always addressed by its one id.
    """

    provider_id: str = Field(
        default=DEFAULT_ARTIFACT_PROVIDER_ID,
        min_length=1,
        description="Id of the ArtifactStorageProvider row that new artifacts are written to.",
    )
