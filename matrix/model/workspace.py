"""Workspace-related Pydantic models.

A workspace is one materialised sandbox: a filesystem root, a shell,
a ``.state/`` git repo, a ``.tmp/`` cache directory, a list of
workspace tools, and a registry of :class:`AgentSession`s currently
running on it.

Models exported:

* :class:`WorkspaceTemplate` -- declarative recipe for materialising a
  workspace. Stored as a ``Document`` in a templates ``Collection``.
* :class:`PackageSpec` -- one package to install during init.
* :class:`FileMount` + :class:`FileSource` -- file seeding shape.
* :class:`ResourceLimits` -- backend-enforced bounds (CPU / mem / network / disk).
* :class:`WorkspaceTemplateOverrides` -- per-instantiation tweaks.
* :class:`FileEntry` -- user-facing file listing entry returned from
  :meth:`Workspace.list_files`.

See ``docs/superpowers/specs/2026-05-02-workspace-design.md`` for the
full design.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, HttpUrl, SecretStr, model_validator

from matrix.model.common import Describeable, Identifiable


# ===========================================================================
# Packages
# ===========================================================================


class PackageSpec(BaseModel):
    """One package to install during workspace init.

    The ``kind`` tag tells the backend which package manager to invoke;
    backends MAY ignore kinds they don't support (with a startup
    warning).
    """

    kind: Literal["apt", "pip", "npm", "cargo", "go", "system"] = Field(
        ...,
        description="Which package manager handles this entry.",
    )
    name: str = Field(
        ...,
        min_length=1,
        description="Package name to install.",
    )
    version: str | None = Field(
        default=None,
        description="Pinned version or version range. None = latest.",
    )


# ===========================================================================
# File mounts (discriminated union over source kinds)
# ===========================================================================


class _InlineSource(BaseModel):
    """File content supplied inline as a string."""

    kind: Literal["inline"] = Field(
        default="inline",
        description="Discriminator tag identifying this as an inline source.",
    )
    content: str = Field(
        ...,
        description="Verbatim file content.",
    )


class _UrlSource(BaseModel):
    """File content fetched from an HTTP(S) URL at materialisation."""

    kind: Literal["url"] = Field(
        default="url",
        description="Discriminator tag identifying this as a URL source.",
    )
    url: HttpUrl = Field(
        ...,
        description="HTTP(S) URL to fetch the content from.",
    )
    sha256: str | None = Field(
        default=None,
        description="Optional SHA-256 of the expected content for integrity verification.",
    )


class _DocumentSource(BaseModel):
    """File content sourced from a Document in a Collection."""

    kind: Literal["document"] = Field(
        default="document",
        description="Discriminator tag identifying this as a Document source.",
    )
    collection_id: str = Field(
        ...,
        min_length=1,
        description="Collection holding the source Document.",
    )
    document_id: str = Field(
        ...,
        min_length=1,
        description="Document id within the collection.",
    )


class _SecretSource(BaseModel):
    """File content sourced from the host secret store."""

    kind: Literal["secret"] = Field(
        default="secret",
        description="Discriminator tag identifying this as a secret-store reference.",
    )
    name: str = Field(
        ...,
        min_length=1,
        description="Reference into the host secret store.",
    )


FileSource = Annotated[
    Union[_InlineSource, _UrlSource, _DocumentSource, _SecretSource],
    Field(discriminator="kind"),
]
"""Type alias: where a :class:`FileMount`'s content comes from.

Discriminated by the ``kind`` field so Pydantic can parse the source
from an untyped dict (e.g. JSON loaded from a template document)
without ambiguity.
"""


class FileMount(BaseModel):
    """One file to seed into the workspace at materialisation."""

    path: str = Field(
        ...,
        min_length=1,
        description="Destination path inside the workspace (relative to the workspace root).",
    )
    source: FileSource = Field(
        ...,
        description="Where the file content comes from.",
    )
    mode: str | None = Field(
        default=None,
        description="Octal mode string (e.g. '0755'). When None, backends use 0644.",
    )


# ===========================================================================
# Resource limits
# ===========================================================================


class ResourceLimits(BaseModel):
    """Backend-enforced bounds on a workspace.

    None means "no limit" for that dimension; the backend SHOULD
    enforce limits where it can (cgroups / Docker resource flags /
    etc.). Backends that cannot enforce a particular bound MAY emit a
    startup warning but MUST NOT fail to materialise a workspace
    purely because they cannot enforce one.
    """

    cpu_cores: float | None = Field(
        default=None,
        description="Maximum CPU cores. None = no limit.",
    )
    memory_bytes: int | None = Field(
        default=None,
        description="Maximum resident memory in bytes. None = no limit.",
    )
    network: Literal["none", "egress", "full"] = Field(
        default="egress",
        description=(
            "Network access mode. 'none' = no network, 'egress' = "
            "outbound only, 'full' = full bidirectional."
        ),
    )
    disk_bytes: int | None = Field(
        default=None,
        description="Maximum disk usage in bytes. None = no limit.",
    )


# ===========================================================================
# Workspace template
# ===========================================================================


class WorkspaceTemplate(Describeable):
    """Declarative recipe for materialising a workspace.

    Stored in a ``workspace_templates`` :class:`Collection`. One
    template can provision many workspaces; each instantiation MAY
    override individual fields (env additions, extra files) at create
    time via :class:`WorkspaceTemplateOverrides`.

    Templates do NOT carry backend-specific connection details (image
    reference, root path, etc.). Instead they reference a configured
    :class:`WorkspaceProvider` by id; the provider supplies the
    backend-specific configuration, the template supplies the
    declarative materialisation recipe.
    """

    provider_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Identifier of the WorkspaceProvider that should materialise "
            "this template. Resolved at workspace-creation time; not "
            "validated here -- the runtime is responsible for matching "
            "the template against a configured provider."
        ),
    )
    packages: list[PackageSpec] = Field(
        default_factory=list,
        description="System / language packages to install during init.",
    )
    files: list[FileMount] = Field(
        default_factory=list,
        description="Files to seed into the workspace at materialisation time.",
    )
    env: dict[str, SecretStr] = Field(
        default_factory=dict,
        description="Environment variables injected for every shell session.",
    )
    init_commands: list[str] = Field(
        default_factory=list,
        description=(
            "Shell commands run once after files / packages land. "
            "Failure of any command is fatal to workspace materialisation."
        ),
    )
    state_path: str = Field(
        default=".state",
        min_length=1,
        description="Path inside the workspace root where the state repo lives.",
    )
    tmp_path: str = Field(
        default=".tmp",
        min_length=1,
        description="Path inside the workspace root where the truncation cache lives.",
    )
    resources: ResourceLimits = Field(
        default_factory=ResourceLimits,
        description="CPU / memory / network bounds the backend SHOULD enforce.",
    )


class WorkspaceTemplateOverrides(BaseModel):
    """Per-instantiation tweaks layered on top of a :class:`WorkspaceTemplate`.

    Override semantics are merge-then-extend:

    * ``env`` -- caller's keys overlay the template's keys (caller wins
      on conflict).
    * ``files`` -- caller's list extends the template's list (both
      apply; later mounts win on path conflict).
    * ``init_commands`` -- caller's list extends the template's list
      (template's commands run first, then caller's).
    """

    env: dict[str, SecretStr] = Field(default_factory=dict)
    files: list[FileMount] = Field(default_factory=list)
    init_commands: list[str] = Field(default_factory=list)


# ===========================================================================
# File entry (user-facing listing)
# ===========================================================================


class FileEntry(BaseModel):
    """User-facing file listing entry.

    Returned from :meth:`Workspace.list_files`. Distinct from the agent-
    facing ``ls`` tool's output -- this is for the user (a UI / CLI),
    not for the agent.
    """

    path: str = Field(
        ...,
        min_length=1,
        description="Path relative to the workspace root.",
    )
    kind: Literal["file", "dir", "symlink"] = Field(
        ...,
        description="Filesystem entry kind.",
    )
    size_bytes: int = Field(
        ...,
        ge=0,
        description="Size in bytes (0 for directories).",
    )
    modified_at: datetime = Field(
        ...,
        description="UTC instant of the last modification.",
    )


# ===========================================================================
# Workspace provider configuration
# ===========================================================================


class WorkspaceProviderType(str, Enum):
    """Supported workspace backends.

    The string value is what gets serialised in configuration so it
    must remain stable across releases. Today only ``local`` exists;
    ``docker`` and ``kubernetes`` will land in future sub-projects.
    """

    LOCAL = "local"


class LocalWorkspaceConfig(BaseModel):
    """Connection settings for the local-FS workspace backend.

    Used by :class:`matrix.workspace.local.LocalWorkspaceBackend`. The
    only knob is the directory under which every workspace is
    materialised; each workspace gets its own subdirectory beneath it.
    """

    path: str = Field(
        ...,
        min_length=1,
        description=(
            "Absolute filesystem path under which workspaces will be "
            "materialised. One subdirectory per workspace is created "
            "underneath."
        ),
    )


class WorkspaceProvider(Identifiable):
    """A configured workspace backend.

    Mirrors the ``LLMProvider`` / ``StorageProviderConfig`` /
    ``VectorStoreProviderConfig`` pattern: an identifiable handle plus
    a discriminated provider enum and a backend-specific ``config``
    sub-model.

    The id is a user-chosen handle; :class:`WorkspaceTemplate` carries
    a ``provider_id`` referencing this entry by id.
    """

    provider: WorkspaceProviderType = Field(
        ...,
        description="Which workspace backend this entry targets.",
    )
    config: LocalWorkspaceConfig = Field(
        ...,
        description="Backend-specific connection settings; must match ``provider``.",
    )

    @model_validator(mode="after")
    def _config_matches_provider(self) -> "WorkspaceProvider":
        if self.provider == WorkspaceProviderType.LOCAL and not isinstance(
            self.config, LocalWorkspaceConfig
        ):
            raise ValueError(
                "provider='local' requires a LocalWorkspaceConfig in 'config'"
            )
        return self


# ===========================================================================
# Persisted Workspace record
# ===========================================================================


class Workspace(Identifiable):
    """Persisted record of a materialised workspace.

    The actual workspace contents (filesystem, ``.state`` repo, live
    sessions) live inside the configured :class:`WorkspaceProvider`'s
    backend; this row is the API-layer book-keeping that lets us
    enumerate workspaces, find which provider/template each one was
    materialised against, and re-attach across process restarts.

    Created by ``POST /v1/workspaces`` and removed by
    ``DELETE /v1/workspaces/{id}``. There is no ``Update`` — workspace
    contents are mutated through the files / sessions sub-APIs, not by
    re-PUTing the row.
    """

    template_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Id of the WorkspaceTemplate this workspace was materialised "
            "from. Snapshot — if the template definition changes later "
            "the workspace keeps a reference to the original id."
        ),
    )
    provider_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Id of the WorkspaceProvider that owns the backend storing "
            "this workspace. Used by the API to look up the correct "
            "backend on every per-workspace operation."
        ),
    )
    overrides: WorkspaceTemplateOverrides | None = Field(
        default=None,
        description=(
            "Optional per-instantiation overrides applied at "
            "materialisation time. Recorded so the original create-time "
            "intent stays inspectable; not consulted on subsequent "
            "operations."
        ),
    )
    created_at: datetime = Field(
        ...,
        description="UTC instant the workspace was materialised.",
    )


# ===========================================================================
# Re-exports
# ===========================================================================


__all__ = [
    "FileEntry",
    "FileMount",
    "FileSource",
    "LocalWorkspaceConfig",
    "PackageSpec",
    "ResourceLimits",
    "Workspace",
    "WorkspaceProvider",
    "WorkspaceProviderType",
    "WorkspaceTemplate",
    "WorkspaceTemplateOverrides",
]
