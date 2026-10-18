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
* :class:`WorkspaceTemplateOverrides` -- per-instantiation tweaks, including
  :class:`WorkspaceResourceOverrides` (a per-workspace CPU / memory / disk size).
* :class:`K8sToleration` + :class:`K8sGpu` -- typed Kubernetes placement fields of
  :class:`KubernetesTemplateConfig`.
* :class:`FileEntry` -- user-facing file listing entry returned from
  :meth:`Workspace.list_files`.
* ``WORKSPACE_ID_PATTERN`` -- the one rule for caller-supplied workspace ids.

See ``docs/superpowers/specs/2026-05-02-workspace-design.md`` for the
full design.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from primer.model.common import Describeable, Identifiable
from primer.model.masked_url import MaskedUserinfoUrl

_log = logging.getLogger(__name__)


# A workspace id names a directory under the local workspace root
# (``LocalWorkspaceBackend`` joins it to the root), a docker container-name
# suffix and a k8s object-name component, and it becomes the durable row id,
# a ``LocalStateRepo`` workspace id and a git trailer value. It is therefore
# ONE DNS-1123 label: lowercase letters, digits and ``-``, starting and
# ending with a letter or digit, at most 63 characters. No slash or dot
# (no traversal, no nesting), no control character. Lowercase only because
# on a case-insensitive filesystem (a macOS dev machine) ``Proj`` and
# ``proj`` would be two rows over ONE directory - and the rule protects
# NEW ids only: a new ``proj`` can still adopt a legacy ``Proj`` directory.
# k8s label VALUES allow uppercase, ``_`` and ``.``; the lowercase
# requirement comes from the Gateway API hostname (RFC 1123) and the k8s
# object names, which - like docker DNS names - also need no ``_`` and an
# alphanumeric end. The rule matches every id the platform generates
# (the bootstrap default ``primer``; the generated ``ws-<hex>``) and the
# generated ids always pass it; existing rows are NOT re-validated - the
# rule bites on create only. Every workspace-create entry (the REST body,
# the ``create_workspace`` tool args, ``WorkspaceRegistry.materialise``)
# enforces it, and the local backend re-asserts the containment it implies
# as defence in depth.
WORKSPACE_ID_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"


# The image a Docker template runs when it names none (platform revamp 2026-10, design 14.2). It is the runtime image the
# Docker adapter started for every template before the typed image was honoured, so the migration that rewrites the stored
# Docker templates writes this value and nothing that worked changes.
DOCKER_RUNTIME_IMAGE = "primer/workspace-runtime:1.1"

# Kubernetes name rules for the template fields that become pod labels, selectors, tolerations, an extended resource name and
# a service account. The patterns are the structure only; a label key and a resource name carry a prefix that is ALSO capped
# at 253 characters, which a regular expression cannot say, so the validators below check it too.
_K8S_NAME = r"[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?"
_K8S_SUBDOMAIN = r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)*"
K8S_LABEL_KEY_PATTERN = rf"^(?:{_K8S_SUBDOMAIN}/)?{_K8S_NAME}$"    # the prefix is also capped at 253 characters
K8S_LABEL_VALUE_PATTERN = rf"^(?:{_K8S_NAME})?$"
K8S_RESOURCE_NAME_PATTERN = rf"^{_K8S_SUBDOMAIN}/{_K8S_NAME}$"     # the prefix is also capped at 253 characters
K8S_SERVICE_ACCOUNT_PATTERN = rf"^{_K8S_SUBDOMAIN}$"               # the whole name is also capped at 253 characters
_K8S_PREFIX_MAX = 253


def _is_k8s_label_key(key: str) -> bool:
    return re.fullmatch(K8S_LABEL_KEY_PATTERN, key) is not None and len(key.rpartition("/")[0]) <= _K8S_PREFIX_MAX


def _is_k8s_label_value(value: str) -> bool:
    return re.fullmatch(K8S_LABEL_VALUE_PATTERN, value) is not None


def _is_k8s_resource_name(name: str) -> bool:
    return re.fullmatch(K8S_RESOURCE_NAME_PATTERN, name) is not None and len(name.rpartition("/")[0]) <= _K8S_PREFIX_MAX


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
    url: MaskedUserinfoUrl = Field(
        ...,
        description=(
            "HTTP(S) URL to fetch the content from. It may carry ``user:password@`` (a private file host): the fetch sends it as Basic auth, and every served copy of the "
            "template (API responses, tool results) shows ``user:**********@`` instead. A PUT of the served body keeps the stored password: for an admin when the scheme, "
            "host, port and user are unchanged, for anyone else only when the whole URL is unchanged (any other change that carries the mask is a 422)."
        ),
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
    _InlineSource | _UrlSource | _DocumentSource | _SecretSource,
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
        description=(
            "Maximum CPU cores. None = no limit. Kubernetes requests and limits. "
            "Docker reads backend.cpu_cores / backend.memory_bytes; Local ignores it."
        ),
    )
    memory_bytes: int | None = Field(
        default=None,
        description=(
            "Maximum resident memory in bytes. None = no limit. Kubernetes requests and limits. "
            "Docker reads backend.cpu_cores / backend.memory_bytes; Local ignores it."
        ),
    )
    network: Literal["none", "egress", "full"] = Field(
        default="egress",
        description=(
            "Ignored: no backend applies a network mode (the Local backend only logs a warning when it is not "
            "'egress'). Kept so rows written before the 2026-10 revamp load."
        ),
    )
    disk_bytes: int | None = Field(
        default=None,
        description=(
            "Ignored: no backend enforces a disk bound from the template (the Local backend only logs a warning). "
            "A Kubernetes disk size is backend.pvc_size, or overrides.resources.disk_bytes for one workspace. "
            "Kept so rows written before the 2026-10 revamp load."
        ),
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
    "rename",
]
"""Allowed values of the ``op`` trailer on state-repo commits."""


class CommitFile(BaseModel):
    """One file touched by a state-repo commit, with its line delta.

    Carries counts only, never content: this is what a history LIST needs
    (a row per file with ``+n/-m``). The actual patch comes from
    ``GET /v1/workspaces/{wid}/commit/{sha}``, fetched per commit when a
    reader opens one, so listing 500 commits never means listing 500 diffs.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., min_length=1, description="Path relative to the .state/ repo root.")
    additions: int = Field(
        default=0,
        ge=0,
        description="Lines added. Always 0 for a binary file -- see ``binary``.",
    )
    deletions: int = Field(
        default=0,
        ge=0,
        description="Lines removed. Always 0 for a binary file -- see ``binary``.",
    )
    binary: bool = Field(
        default=False,
        description=(
            "True when git reported '-' instead of counts, i.e. a non-text "
            "blob. Distinguishes 'no line changes' from 'lines are not a "
            "meaningful unit here', which a bare 0/0 would conflate."
        ),
    )


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
    files: list[CommitFile] | None = Field(
        default=None,
        description=(
            "Files touched by this commit, with line deltas. Populated only "
            "when the caller asks for it (``history(with_files=True)`` / "
            "``GET .../log?with_files=1``) AND the backend can supply it. "
            "``None`` means 'not requested, or this backend cannot tell you' "
            "-- which is deliberately distinct from ``[]``, an empty commit "
            "that genuinely touched no files. A consumer that renders a file "
            "count must not read None as zero."
        ),
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

    Ignored: no backend applies it (the Docker adapter never read it). It is kept on the
    model so rows written before the 2026-10 revamp load, and ``ContainerTemplateConfig.network``
    says so. ``egress`` was meant to toggle whether the workspace can reach outside the
    provider's network.
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
    """Container template variant: owns image, cpu/mem and mounts.

    Image is expected to derive from a primer-runtime base; the platform
    reaches the runtime via WSSandbox + RuntimeClient (Phase 5/6 work).
    When a template names no image it is :data:`DOCKER_RUNTIME_IMAGE`.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["container"] = Field(
        default="container",
        description="Discriminator tag identifying this as container-template config.",
    )
    image: str = Field(
        default=DOCKER_RUNTIME_IMAGE,
        description=(
            "OCI image; must contain the primer workspace runtime (build a custom one FROM "
            f"{DOCKER_RUNTIME_IMAGE}). Defaults to the built-in runtime image."
        ),
    )
    entrypoint: list[str] | None = Field(
        default=None,
        description=(
            "Ignored: the Docker adapter always starts the runtime image's own server. "
            "Kept so rows written before the 2026-10 revamp load."
        ),
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
        description="Optional additional host mounts (operator opt-in). Admin only.",
    )
    network: ContainerNetworkConfig | None = Field(
        default=None,
        description=(
            "Ignored: the container backend never applied a network policy. "
            "Kept so rows written before the 2026-10 revamp load."
        ),
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


class K8sToleration(BaseModel):
    """One pod toleration (platform revamp 2026-10), written to ``spec.tolerations`` by the manifest builder.

    The two rules Kubernetes itself enforces: ``Exists`` takes no value, and a toleration with no key must use
    ``Exists`` (it then matches every taint). ``key`` and ``value`` are label-shaped.
    """

    model_config = ConfigDict(extra="forbid")
    key: str | None = Field(
        default=None,
        description="Taint key to tolerate; null (with operator Exists) tolerates every taint.",
    )
    operator: Literal["Equal", "Exists"] = Field(
        default="Equal",
        description="Equal compares the value; Exists matches any value, so no value may be set.",
    )
    value: str | None = Field(
        default=None,
        description="Taint value to match with the Equal operator; must be empty with Exists.",
    )
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"] | None = Field(
        default=None,
        description="Taint effect to tolerate; null tolerates every effect.",
    )

    @field_validator("key")
    @classmethod
    def _key_is_a_label_key(cls, value: str | None) -> str | None:
        if value is not None and not _is_k8s_label_key(value):
            raise ValueError(
                f"toleration key {value!r} is not a valid Kubernetes label key: use an optional lowercase DNS "
                "prefix and '/', then a name of letters, digits, '-', '_' or '.' (at most 63, starting and "
                "ending with a letter or digit); leave the key out to tolerate every taint"
            )
        return value

    @field_validator("value")
    @classmethod
    def _value_is_a_label_value(cls, value: str | None) -> str | None:
        if value is not None and not _is_k8s_label_value(value):
            raise ValueError(
                f"toleration value {value!r} is not a valid Kubernetes label value: use letters, digits, "
                "'-', '_' or '.' (at most 63, starting and ending with a letter or digit), or leave it empty"
            )
        return value

    @model_validator(mode="after")
    def _operator_rules(self) -> "K8sToleration":
        if self.operator == "Exists" and self.value:
            raise ValueError("a toleration with the Exists operator takes no value")
        if not self.key and self.operator != "Exists":
            raise ValueError("a toleration without a key must use the Exists operator")
        return self


class K8sGpu(BaseModel):
    """GPUs for the workspace container (platform revamp 2026-10).

    The manifest builder adds ``{resource_name: str(count)}`` to the container's resource requests and limits (an
    extended resource has to be requested and limited at the same value).
    """

    model_config = ConfigDict(extra="forbid")
    count: int = Field(..., ge=1, description="How many GPUs to request (at least 1).")
    resource_name: str = Field(
        default="nvidia.com/gpu",
        pattern=K8S_RESOURCE_NAME_PATTERN,
        description="The extended resource name the cluster's device plugin advertises, for example amd.com/gpu.",
    )

    @field_validator("resource_name", mode="before")
    @classmethod
    def _resource_name_is_domain_and_name(cls, value: Any) -> Any:
        # Runs before the pattern check so the message says what the name is, not the regular expression; it also
        # caps the domain at 253 characters, which the pattern cannot.
        if isinstance(value, str) and not _is_k8s_resource_name(value):
            raise ValueError(
                f"GPU resource name {value!r} is not a valid Kubernetes extended resource name: "
                "use <domain>/<name>, for example nvidia.com/gpu (the domain at most 253 characters)"
            )
        return value


class KubernetesTemplateConfig(BaseModel):
    """K8s template variant — system variant. agent_sandbox variant is reserved.

    Image is expected to derive from a primer-runtime base; runtime
    reachability is configured on the workspace *provider*, not the
    template (see KubernetesWorkspaceConfig.reachability).

    The size is ``WorkspaceTemplate.resources`` (requests = limits) and ``pvc_size``. The five placement fields
    (``node_selector``, ``tolerations``, ``gpu``, ``service_account_name``, ``image_pull_policy``) are typed here and
    written by the manifest builder; ``pod_overrides`` still cannot set them. ``tolerations`` and
    ``service_account_name`` are admin-only (``primer.workspace.template_privilege``). The CPU and memory request and
    limit strings, ``network_policy_name`` and ``container_security_context_overrides`` are not applied; they stay so
    that rows written before the 2026-10 revamp load.
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
        description=(
            "Ignored: the pod's CPU comes from the template's resources.cpu_cores (requests = limits). "
            "Kept so rows written before the 2026-10 revamp load."
        ),
    )
    cpu_limit: str | None = Field(
        default=None,
        description=(
            "Ignored: the pod's CPU comes from the template's resources.cpu_cores (requests = limits). "
            "Kept so rows written before the 2026-10 revamp load."
        ),
    )
    memory_request: str | None = Field(
        default=None,
        description=(
            "Ignored: the pod's memory comes from the template's resources.memory_bytes (requests = limits). "
            "Kept so rows written before the 2026-10 revamp load."
        ),
    )
    memory_limit: str | None = Field(
        default=None,
        description=(
            "Ignored: the pod's memory comes from the template's resources.memory_bytes (requests = limits). "
            "Kept so rows written before the 2026-10 revamp load."
        ),
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
        description=(
            "Ignored: the manifest never applied it. Kept so rows written before the 2026-10 revamp load."
        ),
    )

    pod_overrides: dict | None = Field(
        default=None,
        description="Deep-merged into PodSpec at manifest build time.",
    )
    container_security_context_overrides: dict | None = Field(
        default=None,
        description="Not applied; any non-empty value is refused.",
    )

    node_selector: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Kubernetes only: the pod's nodeSelector, label key to label value. Any user may set it. "
            "Every key and value must be a valid Kubernetes label key and value."
        ),
    )
    tolerations: list[K8sToleration] = Field(
        default_factory=list,
        description=(
            "Kubernetes only: the pod's tolerations. Admin only: a toleration lets the pod run on tainted "
            "nodes, which the cluster keeps for its own workloads."
        ),
    )
    gpu: K8sGpu | None = Field(
        default=None,
        description=(
            "Kubernetes only: GPUs for the container, requested and limited as <resource_name>: <count>. "
            "Any user may set it. null = no GPU."
        ),
    )
    service_account_name: str | None = Field(
        default=None,
        pattern=K8S_SERVICE_ACCOUNT_PATTERN,
        max_length=253,
        description=(
            "Kubernetes only: the pod's serviceAccountName. Admin only: the account's token and permissions "
            "reach the workspace. null = the namespace's default account."
        ),
    )
    image_pull_policy: Literal["Always", "IfNotPresent", "Never"] | None = Field(
        default=None,
        description=(
            "Kubernetes only: the container's imagePullPolicy. Any user may set it. "
            "null = the cluster's default for the image tag."
        ),
    )

    @field_validator("service_account_name", mode="before")
    @classmethod
    def _service_account_is_a_dns_name(cls, value: Any) -> Any:
        # Runs before the pattern check so the message says what the name is, not the regular expression.
        if isinstance(value, str) and (len(value) > 253 or re.fullmatch(K8S_SERVICE_ACCOUNT_PATTERN, value) is None):
            raise ValueError(
                f"service account name {value!r} is not valid: use lowercase letters, digits, '-' and '.', "
                "starting and ending with a letter or digit (at most 253 characters)"
            )
        return value

    @field_validator("node_selector")
    @classmethod
    def _node_selector_is_label_shaped(cls, value: dict[str, str]) -> dict[str, str]:
        for key, label in value.items():
            if not _is_k8s_label_key(key):
                raise ValueError(
                    f"node_selector key {key!r} is not a valid Kubernetes label key: use an optional lowercase "
                    "DNS prefix and '/', then a name of letters, digits, '-', '_' or '.' (at most 63, "
                    "starting and ending with a letter or digit)"
                )
            if not _is_k8s_label_value(label):
                raise ValueError(
                    f"node_selector value {label!r} for key {key!r} is not a valid Kubernetes label value: use "
                    "letters, digits, '-', '_' or '.' (at most 63, starting and ending with a letter or "
                    "digit), or leave it empty"
                )
        return value


WorkspaceTemplateBackendConfig = Annotated[
    LocalTemplateConfig | ContainerTemplateConfig | KubernetesTemplateConfig,
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
    strict_write_locking: bool = Field(
        default=False,
        description=(
            "When true, exec and tool write-locks scope to the whole "
            "workspace root instead of the per-workdir subtree, closing the "
            "cross-workdir absolute-path overlap gap at the cost of "
            "parallelism. Default false (workdir-scoped)."
        ),
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


class WorkspaceResourceOverrides(BaseModel):
    """A per-workspace size, chosen at create (platform revamp 2026-10, design 14.3).

    Every field is a positive number or ``None`` ("the template's value"); there is no provider cap, a size a cluster
    cannot schedule fails at the pod wait as a template's would. The names are the ones the backends read: Kubernetes
    ``resources.cpu_cores`` / ``resources.memory_bytes`` (requests = limits) and the PVC request, Docker
    ``backend.cpu_cores`` / ``backend.memory_bytes``.
    """

    model_config = ConfigDict(extra="forbid")
    cpu_cores: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
        description="CPU cores for this workspace. null = the template's value.",
    )
    memory_bytes: int | None = Field(
        default=None,
        gt=0,
        description="Memory in bytes for this workspace. null = the template's value.",
    )
    disk_bytes: int | None = Field(
        default=None,
        gt=0,
        description="Disk (the Kubernetes PVC request) in bytes for this workspace. null = the template's value.",
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
    * ``resources`` -- the workspace's own size; a field that is set
      replaces the template's value (applied at create; which provider
      takes which field is decided in
      ``primer/workspace/size_overrides.py``).
    """

    env: dict[str, SecretStr] = Field(default_factory=dict)
    files: list[FileMount] = Field(default_factory=list)
    init_commands: list[str] = Field(default_factory=list)
    resources: WorkspaceResourceOverrides | None = Field(
        default=None,
        description=(
            "Per-workspace size. Kubernetes takes cpu_cores, memory_bytes and disk_bytes. Docker can take "
            "cpu_cores and memory_bytes (it has no disk quota); Local can take none."
        ),
    )


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
    origin: Literal["collection"] | None = Field(
        default=None,
        description="Set to 'collection' on a directory that is a mounted collection root.",
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
    DockerRuntimeConfig | PodmanRuntimeConfig | ContainerdRuntimeConfig,
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
    ContainerConnectionSocket | ContainerConnectionRemote,
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
    ContainerReachabilityHostPort | ContainerReachabilityBridge,
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
    K8sConnectionInCluster | K8sConnectionKubeconfig | K8sConnectionServiceAccountToken,
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
    K8sGatewayRoutingHostname | K8sGatewayRoutingPath,
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
    K8sReachabilityInCluster | K8sReachabilityIngress | K8sReachabilityGateway,
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
    LocalWorkspaceConfig | ContainerWorkspaceConfig | KubernetesWorkspaceConfig,
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
    anchor: str | None = Field(
        default=None,
        description=(
            "Optional standing room anchor (e.g. a Slack thread ts) the "
            "workspace's outbound replies attach to. None posts to the "
            "channel root."
        ),
    )


class WorkspaceEventsConfig(BaseModel):
    """Opt-in workspace lifecycle events on the platform event log.

    When ``enabled``, the WorkspaceEventBridge holds one runtime
    EVENTS_SUBSCRIBE stream for this workspace and emits
    ``workspace.file_changed`` / ``workspace.exec_started`` /
    ``workspace.exec_exited`` scoped to it. Default None on the row =
    no stream, no events (a busy build tree floods nothing unasked).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    kinds: list[Literal[
        "file_changed", "exec_started", "exec_exited",
    ]] = Field(
        default_factory=lambda: [
            "file_changed", "exec_started", "exec_exited",
        ],
    )
    path_prefixes: list[str] | None = Field(
        default=None,
        description=(
            "Workspace-relative prefixes file events are limited to; "
            "None watches the whole workspace root."
        ),
    )


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
    reply_binding: WorkspaceChannelLink | None = Field(
        default=None,
        description=(
            "Channel this workspace's session traffic (gates / inform / "
            "lifecycle / final result) replies to. The standing form of the "
            "unified reply binding. Mutable post-create."
        ),
    )
    events: WorkspaceEventsConfig | None = Field(
        default=None,
        description=(
            "Opt-in workspace lifecycle event streaming; None = off."
        ),
    )
    terminal_user_access: bool = Field(
        default=False,
        description=(
            "Non-admin access to the workspace's integrated terminal "
            "(``WS /workspaces/{id}/terminal``). The terminal is admin-only "
            "by default; flipping this on admits callers holding at least "
            "the ``user`` role too (``restricted`` never gets a shell). Set "
            "via ``PUT /workspaces/{id}/terminal_access``. Backward-"
            "compatible default for existing rows."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _alias_channel_association(cls, data: Any) -> Any:
        """Back-compat: stored rows may carry the old ``channel_association``
        field. Move it to ``reply_binding`` when the new field is absent."""
        if (
            isinstance(data, dict)
            and "channel_association" in data
            and "reply_binding" not in data
        ):
            data = {**data}
            data["reply_binding"] = data.pop("channel_association")
        return data


# ===========================================================================
# Re-exports
# ===========================================================================


__all__ = [
    "CommitFile",
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
    "DOCKER_RUNTIME_IMAGE",
    "DockerRuntimeConfig",
    "FileEntry",
    "FileMount",
    "FileSource",
    "K8S_LABEL_KEY_PATTERN",
    "K8S_LABEL_VALUE_PATTERN",
    "K8S_RESOURCE_NAME_PATTERN",
    "K8S_SERVICE_ACCOUNT_PATTERN",
    "K8sConnectionConfig",
    "K8sConnectionInCluster",
    "K8sConnectionKubeconfig",
    "K8sConnectionServiceAccountToken",
    "K8sGatewayParentRef",
    "K8sGatewayRouting",
    "K8sGatewayRoutingHostname",
    "K8sGatewayRoutingPath",
    "K8sGpu",
    "K8sReachabilityConfig",
    "K8sReachabilityGateway",
    "K8sReachabilityIngress",
    "K8sReachabilityInCluster",
    "K8sToleration",
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
    "WorkspaceResourceOverrides",
    "WorkspaceRuntimeMeta",
    "WorkspaceStatus",
    "WorkspaceTemplate",
    "WorkspaceTemplateBackendConfig",
    "WorkspaceTemplateOverrides",
]
