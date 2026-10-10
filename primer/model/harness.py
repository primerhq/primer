"""Harness models — see docs/superpowers/specs/2026-05-27-harness-design.md §5."""

from __future__ import annotations

import os
import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, PlainSerializer, SecretStr, SerializationInfo, field_validator

from primer.common.url_userinfo import MaskCannotBeRestored, carries_mask, mask_userinfo, restore_userinfo
from primer.model.common import STORAGE_DUMP_CONTEXT, Identifiable
from primer.model.principal import PrincipalRef


# ---------------------------------------------------------------------------
# git_url / ref rules (security review 2026-10-08, AUTHZ-04 / INJ-03 / FS-02)
# ---------------------------------------------------------------------------

#: Operator opt-in for ``file://`` remotes. Off in production; the test lanes set it because they clone local bare repos.
FILE_GIT_URLS_ENV = "PRIMER_HARNESS_ALLOW_FILE_URLS"

_REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,254}$")


def file_git_urls_allowed() -> bool:
    """Whether ``file://`` remotes are accepted: only when the operator set ``PRIMER_HARNESS_ALLOW_FILE_URLS=1``.

    Read on every call (not cached at import), so tests can flip it and a restart is what changes it in production.
    """
    return os.environ.get(FILE_GIT_URLS_ENV) == "1"


def validate_git_url(url: str) -> str:
    """Return ``url`` if git may be pointed at it, else raise ``ValueError``.

    Only ``https://`` with a host is accepted (plus ``file://`` under the operator opt-in). A value git would read as an option
    (a leading ``-``), a remote-helper transport (``ext::``), a local path, ``ssh``, ``git`` or plain ``http`` is refused, as is
    any whitespace or control character.
    """
    if not isinstance(url, str) or not url:
        raise ValueError("git_url must be a non-empty string")
    if url.startswith("-"):
        raise ValueError("git_url must not start with '-'")
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        raise ValueError("git_url must not contain whitespace or control characters")
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        parsed.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError as exc:
        raise ValueError(f"git_url is not a valid URL: {exc}") from exc
    if parsed.scheme == "https":
        if not host or host.startswith("-"):
            raise ValueError("git_url must name a host (https://host/path)")
        return url
    if parsed.scheme == "file" and file_git_urls_allowed():
        if not parsed.path.startswith("/"):
            raise ValueError("a file:// git_url must be an absolute path")
        return url
    raise ValueError("git_url must be an https:// URL")


def _serialize_git_url(value, info: SerializationInfo):
    """The dump of a git URL: unchanged in a python-mode dump (git reads the real URL), the real URL under the storage context (``dump_for_storage``), and in a JSON-mode dump the URL with the
    password of its userinfo masked (``https://reader:**********@host/org/repo.git``; a lone ``https://TOKEN@host``, the personal-access-token-in-the-username shape, whole).

    The same rule as :data:`primer.model.providers._shared.MaskedUserinfoUrl` (the provider Base URL, ticket 01a11cdf part 3) for a ``str`` field: a git URL is not an ``HttpUrl`` (``file://``
    is allowed under the operator opt-in, and the string is compared and keyed as typed). No return annotation and no ``return_type``: a ``str`` return type makes pydantic 2.13 warn on every
    python-mode dump, with the URL in the text.
    """
    if not info.mode_is_json():
        return value
    context = info.context
    if isinstance(context, dict) and all(context.get(key) == flag for key, flag in STORAGE_DUMP_CONTEXT.items()):
        return str(value)
    return mask_userinfo(str(value))


#: A git URL served without the password of its userinfo (ticket 01a11d32, the harness family). ``Harness.git_url`` and ``ResolvedDependency.git_url`` are served to every user.
MaskedGitUrl = Annotated[str, PlainSerializer(_serialize_git_url, when_used="always")]


class GitUrlMaskRefused(ValueError):
    """A ``git_url`` carries the mask a read serves, but the stored one cannot give the credential back to it (another host, user, scheme or port, or nothing stored), or it is a create."""


_GIT_URL_UNRESTORABLE = "git_url: re-enter the password: the stored one is kept only for the same host and user"


def restore_served_git_url(incoming: str | None, stored: str | None) -> str | None:
    """``incoming`` with the STORED credential put back when it carries the mask a read served for ``stored``, else ``incoming`` as it is.

    A mask is restored ONLY for the remote it was served for: the scheme, host and port equal the stored URL's and the username is the same (:func:`primer.common.url_userinfo.restore_userinfo`). A
    mask that cannot be restored raises :class:`GitUrlMaskRefused`: restoring it would give the credential to whatever host the update names (the same rule as SEC-03 for ``git_token``), and
    keeping it would store the literal mask as the password. A new real password, a removed credential and ``None`` are the person's change and come back as they are.
    """
    if incoming is None:
        return None
    try:
        restored = restore_userinfo(incoming, stored or "")
    except MaskCannotBeRestored as exc:
        raise GitUrlMaskRefused(f"{_GIT_URL_UNRESTORABLE} ({exc})") from None
    return incoming if restored is None else restored


def refuse_served_git_url(url: str | None) -> None:
    """Refuse a NEW harness whose ``git_url`` carries the served mask: there is nothing stored to restore it from (the copy-a-harness move would store the mask as the password)."""
    if url and carries_mask(url):
        raise GitUrlMaskRefused(_GIT_URL_UNRESTORABLE)


def validate_git_ref(ref: str) -> str:
    """Return ``ref`` if it is a plain branch, tag or commit name, else raise ``ValueError``.

    Letters, digits, ``.``, ``_``, ``/`` and ``-``; it may not start with ``-`` (git would read it as an option) or ``.``, and the
    git refname rules that matter here hold: no ``..``, no ``//``, no ``/.``, and no trailing ``/``, ``.`` or ``.lock``.
    """
    if (
        not isinstance(ref, str)
        or not _REF_RE.match(ref)
        or ".." in ref
        or "//" in ref
        or "/." in ref
        or ref.endswith(("/", ".", ".lock"))
    ):
        raise ValueError(
            "ref must be a branch, tag or commit name: letters, digits, '.', '_', '/' and '-', not starting with '-'",
        )
    return ref


class HarnessStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    INSTALLED = "installed"
    OUTDATED = "outdated"
    ERROR = "error"


class HarnessDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class HarnessOperation(str, Enum):
    FETCH = "fetch"
    INSTALL = "install"
    SYNC = "sync"
    UNINSTALL = "uninstall"
    BUILD = "build"
    PUSH = "push"


class OverrideMapping(BaseModel):
    """One field on a tracked entity that becomes an override at render time."""

    field_path: str
    override_path: str
    widget: Literal[
        "llm-provider-picker",
        # An agent's model is a ModelProfile id, so remapping one on install
        # is a single choice rather than a provider + model pair.
        "model-profile-picker",
        "embedding-provider-picker",
        "ssp-picker",
        "cross-encoder-picker",
    ] | None = None
    schema_override: dict[str, Any] | None = None

    @field_validator("field_path")
    @classmethod
    def _fp(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("field_path must be a JSON pointer starting with '/'")
        return v


class TrackedEntity(BaseModel):
    """One entity included in an outbound harness."""

    kind: Literal["agent", "graph", "collection", "document", "toolset"]
    source_id: str
    template_name: str
    overrides: list[OverrideMapping] = Field(default_factory=list)

    @field_validator("template_name")
    @classmethod
    def _tn(cls, v: str) -> str:
        if not re.match(r"^[a-z][a-z0-9-]{0,62}$", v):
            raise ValueError("template_name must match [a-z][a-z0-9-]{0,62}")
        return v


_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_DEP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class DependencyRef(BaseModel):
    """Declared subharness dependency from a parent harness.yaml."""

    name: str = Field(..., min_length=1, max_length=64)
    git_url: str = Field(..., min_length=1)
    ref: str = Field(default="main", min_length=1)
    subpath: str | None = None
    git_token: SecretStr | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _DEP_NAME_RE.match(v):
            raise ValueError(
                "dependency name must match [a-z][a-z0-9-]{0,63}",
            )
        return v

    @field_validator("git_url")
    @classmethod
    def _validate_git_url(cls, v: str) -> str:
        return validate_git_url(v)

    @field_validator("ref")
    @classmethod
    def _validate_ref(cls, v: str) -> str:
        return validate_git_ref(v)


class ResolvedDependency(BaseModel):
    """A dependency node resolved by the transitive walk."""

    name: str
    slug: str
    git_url: MaskedGitUrl
    ref: str
    subpath: str | None = None
    resolved_commit: str
    bundle_hash: str
    depth: int = Field(..., ge=0)
    parent_name: str | None = None


class Harness(Identifiable):
    slug: str = Field(..., min_length=2, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    git_url: MaskedGitUrl | None = Field(default=None, min_length=1)
    git_token: SecretStr | None = None
    subpath: str | None = None
    ref: str = Field(default="main", min_length=1)
    overrides: dict[str, Any] = Field(default_factory=dict)
    overrides_schema: dict[str, Any] | None = None
    overrides_hash: str | None = None
    schema_hash: str | None = None
    resolved_commit: str | None = None
    available_commit: str | None = None
    bundle_hash: str | None = None
    available_bundle_hash: str | None = None
    status: HarnessStatus = HarnessStatus.DRAFT
    commits_ahead: bool = False
    overrides_dirty: bool = False
    schema_missing_input: bool = False
    pending_operation: HarnessOperation | None = None
    uninstall_cascade: bool = Field(
        default=False,
        description=(
            "For an enqueued UNINSTALL (harness delete): also delete the "
            "harness's tracked/managed entities (agents, graphs, collections, "
            "documents, toolsets). When False, removes ONLY the harness row "
            "and its rendering, leaving every tracked entity intact. The "
            "delete endpoint resolves this per request: an explicit "
            "``?cascade=`` wins, otherwise it defaults by direction (inbound "
            "cascades so uninstall removes the installed objects; outbound "
            "does not, keeping the user's own tracked objects)."
        ),
    )
    operation_requested_by: PrincipalRef | None = Field(
        default=None,
        description=(
            "Who enqueued the pending INSTALL or SYNC. The worker applies the toolset admin rule against it: a bundle that "
            "would create or change a stdio MCP toolset is refused unless this requester may (an admin, or the internal "
            "system / trigger actors). ``None`` (unknown) fails closed."
        ),
    )
    last_operation_at: datetime | None = None
    last_operation_error: str | None = None
    dependencies_resolved: list[ResolvedDependency] = Field(default_factory=list)
    direction: HarnessDirection = HarnessDirection.INBOUND
    tracked_entities: list[TrackedEntity] = Field(default_factory=list)
    last_pushed_commit: str | None = None
    last_pushed_bundle_hash: str | None = None
    last_pushed_at: datetime | None = None
    created_at: datetime

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                "slug must match [a-z][a-z0-9-]{1,63}",
            )
        if "__" in v:
            raise ValueError("slug may not contain '__'")
        return v


#: What a read serves for a stored ``git_token`` (the plain ``SecretStr`` mask); sent back, it means "unchanged".
_SERVED_TOKEN_MASK = "**********"


class GitTokenRequired(ValueError):
    """An update that moves ``git_url`` to a new remote while keeping the stored token."""


def apply_git_token_update(
    harness: Harness, *, git_url_set: bool, git_url: str | None, git_token: str | None,
) -> None:
    """Apply an update's ``git_token`` to ``harness`` (in place), refusing to carry the stored token to a new remote.

    ``git_token`` absent (``None``) or the served mask keeps the stored token; ``""`` clears it; any other value replaces it.
    Moving ``git_url`` to a different remote while a token is stored needs the token re-entered (or cleared) in the same update:
    otherwise the next fetch would send the old remote's token to the new host (security review 2026-10-08, SEC-03). Clearing
    the remote (``git_url=None``) sends nothing anywhere, so it needs no token. Raises :class:`GitTokenRequired`; check before
    changing anything else on ``harness``.
    """
    supplied = git_token is not None and git_token != _SERVED_TOKEN_MASK
    moves_remote = git_url_set and git_url is not None and git_url != harness.git_url
    if moves_remote and harness.git_token is not None and not supplied:
        raise GitTokenRequired(
            "changing git_url needs git_token re-entered in the same update (send the token for the new remote, or \"\" to "
            "clear it): the stored token is never sent to a different remote",
        )
    if supplied:
        harness.git_token = SecretStr(git_token) if git_token else None


class RenderedEntry(BaseModel):
    kind: Literal["agent", "graph", "collection", "document", "toolset"]
    template_name: str = Field(..., min_length=1, max_length=64)
    resolved_id: str
    template_source_hash: str
    rendered_hash: str
    rendered_payload: dict[str, Any]
    source_dependency: str | None = None
    source_entity_id: str | None = None


class HarnessRendering(Identifiable):
    harness_id: str
    bundle_hash: str
    overrides_hash: str
    schema_hash: str | None
    entries: list[RenderedEntry]
    rendered_at: datetime


__all__ = [
    "DependencyRef",
    "GitTokenRequired",
    "GitUrlMaskRefused",
    "Harness",
    "HarnessDirection",
    "HarnessOperation",
    "HarnessRendering",
    "HarnessStatus",
    "MaskedGitUrl",
    "OverrideMapping",
    "RenderedEntry",
    "ResolvedDependency",
    "TrackedEntity",
    "apply_git_token_update",
    "file_git_urls_allowed",
    "refuse_served_git_url",
    "restore_served_git_url",
    "validate_git_ref",
    "validate_git_url",
]
