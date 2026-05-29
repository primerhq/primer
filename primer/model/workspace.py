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
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)

from primer.model.common import Describeable, Identifiable


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
# Volume mounts (used by container/k8s template configs)
# ===========================================================================


class VolumeMount(BaseModel):
    """Extra volume to mount into a sandbox.

    The ``source`` field is backend-interpreted: host path for Container,
    named PVC for K8s, etc.
    """

    source: str = Field(..., min_length=1)
    target: str = Field(..., min_length=1)
    read_only: bool = False


# ===========================================================================
# State-repo shared types (used by ABC + every concrete StateRepo impl)
# ===========================================================================


Op = Literal[
    "attach",
    "message",
    "user_instruction",
    "tool_call",
    "tool_result",
    "memory_write",
    "todo_update",
    "status_change",
]
"""Allowed values of the ``op`` trailer on state-repo commits."""


class CommitInfo(BaseModel):
    """One commit in the state repo, with trailers parsed.

    Returned from :meth:`primer.int.workspace.Workspace.log` and from
    every concrete ``StateRepo.history`` implementation. Lives in the
    model layer so both the ABC and the concrete impls depend on the
    model rather than on each other.
    """

    sha: str = Field(..., description="Full 40-character commit SHA.")
    subject: str = Field(..., description="Commit message subject line.")
    committed_at: datetime = Field(..., description="Committer timestamp (UTC).")
    workspace_id: str | None = Field(
        default=None,
        description="Value of the X-Primer-Workspace trailer, if present.",
    )
    session_id: str | None = Field(
        default=None,
        description="Value of the X-Primer-Session trailer, if present.",
    )
    agent_id: str | None = Field(
        default=None,
        description="Value of the X-Primer-Agent trailer, if present.",
    )
    op: str | None = Field(
        default=None,
        description="Value of the X-Primer-Op trailer, if present.",
    )
    tool: str | None = Field(
        default=None,
        description="Value of the X-Primer-Tool trailer, if present.",
    )
    call_id: str | None = Field(
        default=None,
        description="Value of the X-Primer-Call trailer, if present.",
    )


# ===========================================================================
# Per-backend template configs (discriminated by ``kind``)
# ===========================================================================


class LocalTemplateConfig(BaseModel):
    """Local-backend template config (no backend-specific fields today)."""

    kind: Literal["local"] = "local"


class ContainerTemplateConfig(BaseModel):
    """Container-backend template config."""

    kind: Literal["container"] = "container"
    image: str = Field(..., min_length=1)
    entrypoint: list[str] | None = Field(
        default=None,
        description='Override container PID 1. Default ["sleep", "infinity"].',
    )
    user: str | None = Field(
        default=None,
        description=(
            "Container user, e.g. 'root' or 'uid:gid'. Default = host UID:GID."
        ),
    )
    workdir: str = Field(default="/workspace")
    extra_mounts: list[VolumeMount] = Field(default_factory=list)
    extra_volume_size: str | None = Field(
        default=None,
        description=(
            "Workspace volume size hint (advisory; not all runtimes enforce)."
        ),
    )


class KubernetesTemplateConfig(BaseModel):
    """Kubernetes-backend template config."""

    kind: Literal["kubernetes"] = "kubernetes"
    image: str = Field(..., min_length=1)
    entrypoint: list[str] | None = None
    args: list[str] | None = None
    workdir: str = Field(default="/workspace")
    pvc_size: str = Field(default="10Gi")
    pvc_access_modes: list[str] = Field(
        default_factory=lambda: ["ReadWriteOnce"]
    )
    extra_volume_mounts: list[dict[str, Any]] = Field(default_factory=list)
    extra_volumes: list[dict[str, Any]] = Field(default_factory=list)
    container_overrides: dict[str, Any] = Field(default_factory=dict)
    pod_overrides: dict[str, Any] = Field(default_factory=dict)


WorkspaceTemplateBackendConfig = Annotated[
    Union[
        LocalTemplateConfig,
        ContainerTemplateConfig,
        KubernetesTemplateConfig,
    ],
    Field(discriminator="kind"),
]


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
    backend: WorkspaceTemplateBackendConfig = Field(
        default_factory=lambda: LocalTemplateConfig(),
        description=(
            "Per-backend recipe fields. Must match the provider type "
            "the template targets."
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

    @field_validator("state_path", "tmp_path")
    @classmethod
    def _validate_workspace_relative_path(cls, value: str) -> str:
        # state_path and tmp_path MUST land INSIDE the workspace root.
        # Reject absolute paths (pathlib's `root / "/etc/foo"` returns
        # `/etc/foo`, escaping the root) and any `..` segments (would
        # walk up out of the workspace root). Templates may be authored
        # on one OS and materialised on another, so we reject both
        # POSIX and Windows absolute shapes regardless of platform.
        from pathlib import PurePosixPath, PureWindowsPath
        if (
            PurePosixPath(value).is_absolute()
            or PureWindowsPath(value).is_absolute()
        ):
            raise ValueError(
                f"workspace path {value!r} must be relative to the "
                f"workspace root, not absolute"
            )
        # Normalise separators then split so `foo\..\bar` is rejected
        # alongside `foo/../bar`.
        parts = value.replace("\\", "/").split("/")
        if any(p == ".." for p in parts):
            raise ValueError(
                f"workspace path {value!r} must not contain '..' "
                f"segments (would escape the workspace root)"
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def _default_backend_for_legacy(cls, data: Any) -> Any:
        """Templates serialised before this change lack ``backend``; default
        to the local recipe so old configs keep parsing."""
        if isinstance(data, dict) and "backend" not in data:
            data = {**data, "backend": {"kind": "local"}}
        return data


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
# Workspace status (universal across backends)
# ===========================================================================


class WorkspaceStatus(BaseModel):
    """Backend-agnostic workspace health snapshot.

    Returned from :meth:`primer.int.workspace.Workspace.status`. The
    ``state`` field is the universal vocabulary; ``detail`` carries
    backend-specific extras (container id, pod phase, etc.) the caller
    can render verbatim.
    """

    state: Literal[
        "ready", "starting", "stopped", "unreachable", "destroyed"
    ] = Field(..., description="Universal workspace state.")
    backend: Literal["local", "container", "kubernetes"] = Field(
        ...,
        description="Which backend this workspace is materialised on.",
    )
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description="Backend-specific extras (image id, pod phase, ...).",
    )


# ===========================================================================
# Workspace provider configuration
# ===========================================================================


class WorkspaceProviderType(str, Enum):
    """Supported workspace backends.

    The string value is what gets serialised in configuration so it
    must remain stable across releases.
    """

    LOCAL = "local"
    CONTAINER = "container"
    KUBERNETES = "kubernetes"


class LocalWorkspaceConfig(BaseModel):
    """Connection settings for the local-FS workspace backend.

    Used by :class:`primer.workspace.local.LocalWorkspaceBackend`. The
    only knob is the directory under which every workspace is
    materialised; each workspace gets its own subdirectory beneath it.
    """

    kind: Literal["local"] = Field(
        default="local",
        description="Discriminator tag for the provider-config union.",
    )
    path: str = Field(
        ...,
        min_length=1,
        description=(
            "Absolute filesystem path under which workspaces will be "
            "materialised. One subdirectory per workspace is created "
            "underneath."
        ),
    )


# ---- Container runtime configs (discriminated by ``kind``) ----------------


class DockerRuntimeConfig(BaseModel):
    kind: Literal["docker"] = "docker"
    socket: str | None = Field(
        default=None,
        description=(
            "Override Docker socket path/URL. None = $DOCKER_HOST else "
            "/var/run/docker.sock."
        ),
    )
    api_version: str | None = Field(
        default=None,
        description="Override Docker API version. None = client default.",
    )


class PodmanRuntimeConfig(BaseModel):
    kind: Literal["podman"] = "podman"
    socket: str | None = Field(
        default=None,
        description=(
            "Override Podman socket path. None = $XDG_RUNTIME_DIR/podman/"
            "podman.sock else /run/user/$UID/podman/podman.sock."
        ),
    )


class ContainerdRuntimeConfig(BaseModel):
    kind: Literal["containerd"] = "containerd"
    socket: str = Field(
        default="/run/containerd/containerd.sock",
        description="Containerd CRI socket path.",
    )
    namespace: str = Field(
        default="default",
        description="Containerd namespace (NOT Kubernetes namespace).",
    )


ContainerRuntimeConfig = Annotated[
    Union[DockerRuntimeConfig, PodmanRuntimeConfig, ContainerdRuntimeConfig],
    Field(discriminator="kind"),
]


class ContainerConnectionSocket(BaseModel):
    kind: Literal["socket"] = "socket"
    socket_path: str = Field(..., description="Unix socket to the container runtime.")


class ContainerConnectionRemote(BaseModel):
    kind: Literal["remote"] = "remote"
    url: str = Field(..., description="Remote runtime endpoint, e.g. tcp://docker:2375.")
    tls_ca: str | None = Field(default=None, description="PEM CA cert for mTLS.")
    tls_cert: str | None = Field(default=None, description="PEM client cert for mTLS.")
    tls_key: SecretStr | None = Field(default=None, description="PEM client key for mTLS.")


ContainerConnectionConfig = Annotated[
    Union[ContainerConnectionSocket, ContainerConnectionRemote],
    Field(discriminator="kind"),
]


class ContainerReachabilityHostPort(BaseModel):
    kind: Literal["host_port"] = "host_port"
    bind_host: str = Field(
        default="127.0.0.1",
        description="Host interface the container port maps to. 127.0.0.1 keeps it loopback-only.",
    )


class ContainerReachabilityBridge(BaseModel):
    kind: Literal["bridge_network"] = "bridge_network"
    network_name: str = Field(
        ...,
        description=(
            "Docker network shared by the platform container and workspace containers. "
            "Workspaces are reached via the container hostname; no port mapping is needed."
        ),
    )


ContainerReachabilityConfig = Annotated[
    Union[ContainerReachabilityHostPort, ContainerReachabilityBridge],
    Field(discriminator="kind"),
]


class ContainerWorkspaceConfig(BaseModel):
    """Container provider — connection to the runtime + reachability mode only.

    Image, entrypoint, mounts, resource limits, etc. moved to the template
    in this redesign (see ContainerTemplateConfig)."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["container"] = "container"
    runtime: Literal["docker", "podman", "containerd"] = Field(
        ..., description="Container runtime to use.",
    )
    connection: ContainerConnectionConfig = Field(
        ..., description="How the platform reaches the runtime API.",
    )
    reachability: ContainerReachabilityConfig = Field(
        ..., description="How the platform reaches primer-runtime inside each workspace.",
    )
    image_pull_secrets: list[str] = Field(
        default_factory=list,
        description="Optional list of registry-auth secret refs.",
    )


class KubernetesWorkspaceConfig(BaseModel):
    """Settings for KubernetesWorkspaceBackend."""

    kind: Literal["kubernetes"] = "kubernetes"
    in_cluster: bool = Field(
        default=False,
        description="If True, use in-cluster kubeconfig.",
    )
    kubeconfig_path: str | None = Field(
        default=None,
        description="Path to a kubeconfig file. Ignored when in_cluster=True.",
    )
    context: str | None = Field(
        default=None,
        description="kubeconfig context name. None = current context.",
    )
    namespace: str = Field(
        default="default",
        description="Kubernetes namespace for all resources.",
    )
    name_prefix: str = Field(
        default="primer-ws-",
        description="StatefulSet/PVC name prefix.",
    )
    storage_class: str | None = Field(
        default=None,
        description="StorageClass for PVCs. None = cluster default.",
    )
    default_pvc_size: str = Field(
        default="10Gi",
        description="Default PVC size when template does not override.",
    )
    service_account: str | None = Field(
        default=None,
        description="ServiceAccount for the workspace pods.",
    )
    image_pull_secrets: list[str] = Field(
        default_factory=list,
        description="Image pull secret names.",
    )
    pull_policy: Literal["Always", "IfNotPresent", "Never"] = Field(
        default="IfNotPresent",
        description="K8s container imagePullPolicy.",
    )
    pod_security_context: dict[str, Any] | None = Field(
        default=None,
        description="Passthrough to PodSpec.securityContext.",
    )
    container_security_context: dict[str, Any] | None = Field(
        default=None,
        description="Passthrough to Container.securityContext.",
    )
    node_selector: dict[str, str] = Field(
        default_factory=dict,
        description="Passthrough to PodSpec.nodeSelector.",
    )
    tolerations: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Passthrough to PodSpec.tolerations.",
    )
    annotations: dict[str, str] = Field(
        default_factory=dict,
        description="Annotations applied to StatefulSet + Pod.",
    )
    labels: dict[str, str] = Field(
        default_factory=dict,
        description="Labels applied to StatefulSet + Pod.",
    )


WorkspaceProviderConfig = Annotated[
    Union[
        LocalWorkspaceConfig,
        ContainerWorkspaceConfig,
        KubernetesWorkspaceConfig,
    ],
    Field(discriminator="kind"),
]


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
    config: WorkspaceProviderConfig = Field(
        ...,
        description="Backend-specific connection settings; must match ``provider``.",
    )

    @model_validator(mode="after")
    def _config_matches_provider(self) -> "WorkspaceProvider":
        expected_kind = {
            WorkspaceProviderType.LOCAL: "local",
            WorkspaceProviderType.CONTAINER: "container",
            WorkspaceProviderType.KUBERNETES: "kubernetes",
        }[self.provider]
        if self.config.kind != expected_kind:
            raise ValueError(
                f"provider={self.provider.value!r} requires "
                f"config.kind={expected_kind!r}, got {self.config.kind!r}"
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
    "CommitInfo",
    "ContainerConnectionConfig",
    "ContainerConnectionRemote",
    "ContainerConnectionSocket",
    "ContainerdRuntimeConfig",
    "ContainerReachabilityBridge",
    "ContainerReachabilityConfig",
    "ContainerReachabilityHostPort",
    "ContainerRuntimeConfig",
    "ContainerTemplateConfig",
    "ContainerWorkspaceConfig",
    "DockerRuntimeConfig",
    "FileEntry",
    "FileMount",
    "FileSource",
    "KubernetesTemplateConfig",
    "KubernetesWorkspaceConfig",
    "LocalTemplateConfig",
    "LocalWorkspaceConfig",
    "Op",
    "PackageSpec",
    "PodmanRuntimeConfig",
    "ResourceLimits",
    "VolumeMount",
    "Workspace",
    "WorkspaceProvider",
    "WorkspaceProviderConfig",
    "WorkspaceProviderType",
    "WorkspaceStatus",
    "WorkspaceTemplate",
    "WorkspaceTemplateBackendConfig",
    "WorkspaceTemplateOverrides",
]
