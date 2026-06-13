"""Workspace-related Pydantic models.

A workspace is one materialised sandbox: a filesystem root, a shell,
a ``.state/`` git repo, a ``.tmp/`` cache directory, a list of
workspace tools, and a registry of :class:`AgentSession`s currently
running on it.

Models exported:

* :class:`WorkspaceTemplate` -- declarative recipe for materialising a
  workspace. Stored as a ``Document`` in a templates ``Collection``.
* :class:`FileMount` + :class:`FileSource` -- file seeding shape.
* :class:`ResourceLimits` -- backend-enforced bounds (CPU / mem / network / disk).
* :class:`WorkspaceTemplateOverrides` -- per-instantiation tweaks.
* :class:`FileEntry` -- user-facing file listing entry returned from
  :meth:`Workspace.list_files`.

See ``docs/superpowers/specs/2026-05-02-workspace-design.md`` for the
full design.
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, ClassVar, Literal, Union

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

_log = logging.getLogger(__name__)


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


class ContainerMount(BaseModel):
    """Host → container mount declaration (template-owned)."""

    model_config = ConfigDict(extra="forbid")
    host: str = Field(..., description="Path on the host (provider's runtime).")
    container: str = Field(..., description="Mount point inside the container.")
    readonly: bool = Field(default=False, description="Mount read-only.")


class ContainerNetworkConfig(BaseModel):
    """Container network policy.

    ``egress`` toggles whether the workspace can reach outside the provider's
    network. Best-effort: docker/podman support ``--internal`` networks
    (deny_all); containerd is CNI-dependent.
    """

    model_config = ConfigDict(extra="forbid")
    egress: Literal["allow_all", "deny_all"] | None = Field(
        default=None,
        description=(
            "allow_all → default bridge (internet reachable); "
            "deny_all → docker --internal network (workspace-internal only); "
            "null → runtime default."
        ),
    )


class ContainerTemplateConfig(BaseModel):
    """Container template variant — owns image, cpu/mem, mounts, network.

    Image is expected to derive from a primer-runtime base; the platform
    reaches the runtime via WSSandbox + RuntimeClient (Phase 5/6 work).
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["container"] = Field(
        default="container",
        description="Discriminator tag identifying this as container-template config.",
    )
    image: str = Field(
        ...,
        description="OCI image; expected to derive from a primer-runtime base.",
    )
    entrypoint: list[str] | None = Field(
        default=None,
        description="Override the image's ENTRYPOINT; null = use image default.",
    )
    user: str | None = Field(
        default=None,
        description="uid:gid override; null = image default.",
    )
    workdir: str = Field(
        default="/workspace",
        description="Container workdir.",
    )
    cpu_cores: float | None = Field(
        default=None,
        description="docker --cpus value; null = unlimited.",
    )
    memory_bytes: int | None = Field(
        default=None,
        description="docker --memory value in bytes; null = unlimited.",
    )
    extra_mounts: list[ContainerMount] = Field(
        default_factory=list,
        description="Optional additional host mounts (operator opt-in).",
    )
    network: ContainerNetworkConfig | None = Field(
        default=None,
        description="Network policy; null = runtime default.",
    )


class K8sVolume(BaseModel):
    """K8s Volume spec; passthrough to k8s. Forbidden 'name' must be present;
    other fields (configMap/secret/emptyDir/etc.) are validated by k8s itself."""
    model_config = ConfigDict(extra="allow")
    name: str


class K8sVolumeMount(BaseModel):
    """K8s VolumeMount spec; passthrough. `name` + `mountPath` required."""
    model_config = ConfigDict(extra="allow")
    name: str
    mountPath: str
    readOnly: bool | None = None


class KubernetesTemplateConfig(BaseModel):
    """K8s template variant — system variant. agent_sandbox variant is reserved.

    Image is expected to derive from a primer-runtime base; runtime
    reachability is configured on the workspace *provider*, not the
    template (see KubernetesWorkspaceConfig.reachability).
    """
    model_config = ConfigDict(extra="forbid")
    kind: Literal["kubernetes"] = Field(
        default="kubernetes",
        description="Discriminator tag identifying this as k8s-template config.",
    )
    image: str = Field(
        ...,
        description="OCI image; primer-runtime derivative.",
    )
    entrypoint: list[str] | None = Field(default=None)
    args: list[str] | None = Field(default=None)
    workdir: str = Field(default="/workspace")

    cpu_request: str | None = Field(
        default=None,
        description="K8s resource request, e.g. '500m'.",
    )
    cpu_limit: str | None = Field(
        default=None,
        description="K8s resource limit, e.g. '2'.",
    )
    memory_request: str | None = Field(
        default=None,
        description="e.g. '1Gi'.",
    )
    memory_limit: str | None = Field(
        default=None,
        description="e.g. '4Gi'.",
    )

    pvc_size: str = Field(
        default="10Gi",
        description="PVC size declared on the StatefulSet's volumeClaimTemplates.",
    )
    pvc_access_modes: list[Literal["ReadWriteOnce", "ReadWriteMany"]] = Field(
        default_factory=lambda: ["ReadWriteOnce"],
    )
    storage_class: str | None = Field(
        default=None,
        description="StorageClass name; null = cluster default.",
    )

    extra_volumes: list[K8sVolume] = Field(default_factory=list)
    extra_volume_mounts: list[K8sVolumeMount] = Field(default_factory=list)

    network_policy_name: str | None = Field(
        default=None,
        description="Name of a NetworkPolicy operators pre-create in the namespace.",
    )

    pod_overrides: dict | None = Field(
        default=None,
        description="Deep-merged into PodSpec at manifest build time.",
    )
    container_security_context_overrides: dict | None = Field(default=None)


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

    _id_prefix: ClassVar[str] = "workspace-template"

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
            "Shell commands run once after files land. "
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

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_packages(cls, data: Any) -> Any:
        """Silently drop the legacy ``packages`` field with a WARNING log.

        Pre-redesign rows in storage may carry ``packages: [...]``; this
        validator strips it before Pydantic field validation so the row
        loads cleanly and operators see one warning per legacy row.
        """
        if isinstance(data, dict) and "packages" in data:
            removed = data.pop("packages")
            _log.warning(
                "Dropping legacy 'packages' field from WorkspaceTemplate id=%s: %r — "
                "redesign §6.1 removes runtime package install in favour of image-as-BOM.",
                data.get("id"), removed,
            )
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


class WorkspaceDiagnosticResult(BaseModel):
    """Result of one :meth:`primer.int.workspace.Workspace.diagnostic_exec` call.

    Used by the diagnostic endpoint
    (``POST /v1/workspaces/{id}/diagnostic``) to confirm a workspace is
    reachable end-to-end ("hello-world" smoke). Mirrors the shape of
    :class:`primer.int.sandbox.ExecResult` but lives in the model
    package so the API surface doesn't pull in the sandbox ABC.
    """

    stdout: str = Field(default="", description="UTF-8 decoded stdout.")
    stderr: str = Field(default="", description="UTF-8 decoded stderr.")
    exit_code: int = Field(
        ...,
        description="Process exit code (-1 if killed by timeout).",
    )
    duration_seconds: float = Field(
        default=0.0,
        ge=0.0,
        description="Wall-clock time the command took, in seconds.",
    )


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
    """Local provider — host-filesystem workspaces under a single root.

    Workdir / exec timeouts / package management etc. moved off this
    config; local templates own materialisation. The only thing the
    provider needs to know is where to put workspace directories.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["local"] = Field(
        default="local",
        description="Discriminator tag identifying this as local-workspace config.",
    )
    root_path: str = Field(
        default="~/.primer/workspaces",
        description="Root directory under which each workspace gets its own subdirectory. Tilde-expanded.",
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


class K8sConnectionInCluster(BaseModel):
    """In-cluster service-account auth — no extra fields needed."""

    kind: Literal["in_cluster"] = Field(
        default="in_cluster",
        description="Discriminator tag identifying this as in-cluster auth.",
    )


class K8sConnectionKubeconfig(BaseModel):
    """Kubeconfig-based auth — path + optional context."""

    kind: Literal["kubeconfig"] = Field(
        default="kubeconfig",
        description="Discriminator tag identifying this as kubeconfig auth.",
    )
    path: str = Field(..., description="Path to the kubeconfig file (tilde-expanded).")
    context: str | None = Field(
        default=None,
        description="Named context to select; null uses the file's current-context.",
    )


class K8sConnectionServiceAccountToken(BaseModel):
    """Direct service-account-token auth for out-of-cluster setups."""

    kind: Literal["service_account_token"] = Field(
        default="service_account_token",
        description="Discriminator tag identifying this as service-account-token auth.",
    )
    apiserver_url: str = Field(..., description="https://<host>:<port> apiserver URL.")
    ca_data: str = Field(..., description="PEM cluster CA cert (multi-line string).")
    token: SecretStr = Field(..., description="Bearer token for the service account.")
    namespace: str = Field(
        default="default",
        description="Namespace claimed by the token; informational.",
    )


K8sConnectionConfig = Annotated[
    Union[K8sConnectionInCluster, K8sConnectionKubeconfig, K8sConnectionServiceAccountToken],
    Field(discriminator="kind"),
]


class K8sReachabilityInCluster(BaseModel):
    """Platform IS in the same cluster — use headless-service DNS."""

    kind: Literal["in_cluster"] = Field(
        default="in_cluster",
        description="Discriminator tag identifying this as in-cluster reachability.",
    )


class K8sReachabilityIngress(BaseModel):
    """Platform OUT of cluster — operator-supplied ingress URL pattern."""

    kind: Literal["ingress"] = Field(
        default="ingress",
        description="Discriminator tag identifying this as ingress reachability.",
    )
    url_template: str = Field(
        ...,
        description=(
            "wss:// URL template with {workspace_id} placeholder. "
            "The platform substitutes the id at attach-time."
        ),
    )


class K8sGatewayParentRef(BaseModel):
    """Reference to a pre-created Gateway the per-workspace HTTPRoute attaches to."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., description="Gateway resource name.")
    namespace: str | None = Field(
        default=None,
        description="Gateway namespace; null means the workspace namespace.",
    )
    section_name: str | None = Field(
        default=None,
        description="Optional listener (sectionName) on the Gateway to bind to.",
    )


class K8sGatewayRoutingHostname(BaseModel):
    """Route by per-workspace hostname (Host header). Needs wildcard DNS."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["hostname"] = Field(
        default="hostname",
        description="Discriminator: per-workspace hostname routing.",
    )
    hostname_template: str = Field(
        ...,
        description="Hostname with {workspace_id}, e.g. '{workspace_id}.ws.local'.",
    )


class K8sGatewayRoutingPath(BaseModel):
    """Route by shared hostname + path prefix, rewritten to '/'."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["path_prefix"] = Field(
        default="path_prefix",
        description="Discriminator: shared-host path-prefix routing.",
    )
    hostname: str = Field(..., description="Shared host, e.g. 'ws.local'.")
    path_template: str = Field(
        default="/ws/{workspace_id}",
        description="Path prefix with {workspace_id}; stripped to '/' via URLRewrite.",
    )


K8sGatewayRouting = Annotated[
    Union[K8sGatewayRoutingHostname, K8sGatewayRoutingPath],
    Field(discriminator="kind"),
]


class K8sReachabilityGateway(BaseModel):
    """Platform OUT of cluster -- backend creates a Gateway API HTTPRoute per
    workspace pod. The operator pre-creates the Gateway (and GatewayClass);
    this config references it."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["gateway_httproute"] = Field(
        default="gateway_httproute",
        description="Discriminator tag identifying this as gateway-httproute reachability.",
    )
    gateway: K8sGatewayParentRef = Field(
        ..., description="The Gateway the per-workspace HTTPRoute attaches to.",
    )
    routing: K8sGatewayRouting = Field(
        ..., description="Routing strategy: hostname or path_prefix.",
    )
    scheme: Literal["ws", "wss"] = Field(
        default="ws", description="WebSocket scheme the platform dials.",
    )
    external_port: int = Field(
        ..., ge=1, le=65535, description="Port the platform dials (Gateway/entrypoint reachable port).",
    )
    backend_port: int = Field(
        default=5959, ge=1, le=65535, description="Workspace Service port the HTTPRoute targets.",
    )


K8sReachabilityConfig = Annotated[
    Union[K8sReachabilityInCluster, K8sReachabilityIngress, K8sReachabilityGateway],
    Field(discriminator="kind"),
]


class KubernetesWorkspaceConfig(BaseModel):
    """Kubernetes provider — connection + reachability + variant slot.

    The ``variant`` field reserves space for an ``agent_sandbox`` variant that
    materialises workspaces as Sandbox CRDs instead of StatefulSets.
    Only ``system`` (StatefulSet+PVC+Headless Service+Secret) is implemented
    in v1; ``agent_sandbox`` is accepted on provider create but workspace
    create returns 501.

    Image, resources, storage class, security context, network policies,
    pod overrides etc. moved to the template (see :class:`KubernetesTemplateConfig`).
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["kubernetes"] = Field(
        default="kubernetes",
        description="Substrate discriminator (matches WorkspaceProviderConfig union).",
    )
    variant: Literal["system", "agent_sandbox"] = Field(
        default="system",
        description=(
            "Which k8s materialisation strategy to use. `system` creates a "
            "StatefulSet+PVC; `agent_sandbox` (reserved) will create a Sandbox CRD."
        ),
    )
    connection: K8sConnectionConfig = Field(
        ..., description="How the platform reaches the kube apiserver.",
    )
    namespace: str = Field(
        ..., description="Namespace where workspaces are created.",
    )
    reachability: K8sReachabilityConfig = Field(
        ..., description="How the platform reaches primer-runtime inside workspace pods.",
    )
    image_pull_secrets: list[str] = Field(
        default_factory=list,
        description="Names of pre-created imagePullSecrets in the namespace.",
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

    _id_prefix: ClassVar[str] = "workspace-provider"

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


class WorkspaceRuntimeMeta(BaseModel):
    """Per-workspace runtime connection metadata.

    Carries the WS URL + bearer token + (optional) discovered fields the
    platform needs to reach the workspace's primer-runtime instance.

    Persisted alongside the Workspace row; secret-redacted on GET.
    """
    model_config = ConfigDict(extra="forbid")
    url: str = Field(..., description="ws[s]:// URL the platform connects to.")
    token: SecretStr = Field(..., description="Bearer for RuntimeClient.")
    mapped_host_port: int | None = Field(
        default=None,
        description="Container/host_port reachability only: host port mapped to container's 5959.",
    )
    k8s_object_name: str | None = Field(
        default=None,
        description="K8s only: hashed-if-needed object name used for service/sts/secret.",
    )


class WorkspaceChannelLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel_id: str = Field(..., description="The room-Channel this workspace forwards gates to.")


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

    name: str | None = Field(
        default=None,
        description=(
            "Optional human-readable label for the workspace. Operator-"
            "facing only; the id remains the stable handle used by every "
            "API. Set at create time or via the rename route; the console "
            "shows it in place of the id where present."
        ),
    )
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
    phase: Literal["pending", "running", "failed", "terminating"] = Field(
        default="pending",
        description="Lifecycle state driven by the probe task in primer/workspace/probe.py.",
    )
    last_probe_at: datetime | None = Field(default=None)
    last_probe_ok: bool = Field(default=False)
    failure_reason: str | None = Field(
        default=None,
        description="Populated when phase=failed; one-line cause.",
    )
    runtime_meta: WorkspaceRuntimeMeta = Field(
        ...,
        description="Connection coordinates for the runtime inside this workspace.",
    )
    channel_association: WorkspaceChannelLink | None = Field(
        default=None,
        description="Channel this workspace's session gates forward to. Mutable post-create.",
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
    "ContainerMount",
    "ContainerNetworkConfig",
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
    "K8sConnectionConfig",
    "K8sConnectionInCluster",
    "K8sConnectionKubeconfig",
    "K8sConnectionServiceAccountToken",
    "K8sGatewayParentRef",
    "K8sGatewayRouting",
    "K8sGatewayRoutingHostname",
    "K8sGatewayRoutingPath",
    "K8sReachabilityConfig",
    "K8sReachabilityGateway",
    "K8sReachabilityIngress",
    "K8sReachabilityInCluster",
    "K8sVolume",
    "K8sVolumeMount",
    "KubernetesTemplateConfig",
    "KubernetesWorkspaceConfig",
    "LocalTemplateConfig",
    "LocalWorkspaceConfig",
    "Op",
    "PodmanRuntimeConfig",
    "ResourceLimits",
    "VolumeMount",
    "Workspace",
    "WorkspaceChannelLink",
    "WorkspaceDiagnosticResult",
    "WorkspaceProvider",
    "WorkspaceProviderConfig",
    "WorkspaceProviderType",
    "WorkspaceRuntimeMeta",
    "WorkspaceStatus",
    "WorkspaceTemplate",
    "WorkspaceTemplateBackendConfig",
    "WorkspaceTemplateOverrides",
]
