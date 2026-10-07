"""Pre-write checks for :class:`~primer.model.provider.Toolset`, shared by the REST router and the system tools (task 01a111d1, D5
phase 2b).

Moved out of ``primer/api/routers/providers.py``: a python toolset's source must register (registration is where a bad tool must
fail; calling time is too late, an agent mid-turn should never be the thing that discovers a docstring is missing), and its
``source_version`` is SERVER-owned. The router's hooks re-raise the exact ``HTTPException`` they always raised; the system
``create_`` / ``update_toolset`` tools answer ``validation-error``.

Also here: :func:`toolset_admin_reason`, the one rule for which toolset writes only an admin may make (architecture review A-02;
security sweep AUTHZ-01, SSRF-02, SEC-02). Both writers apply it: the router's pre-write hooks (``require_admin``) and the system CRUD tools (the caller's
``ToolContext``), so the two surfaces cannot disagree about it.

Deliberately NOT here: the router's reachability probe for http / sse MCP toolsets (an 8 s outbound call, bypassed by
``?allow_unreachable``). A tool runs inside an agent turn, so it does not probe; the difference is documented in rest-api.md and in
the tool descriptions, and pinned by a test.
"""

from __future__ import annotations

from primer.common.entity_checks import EntityCheckError
from primer.model.common import preserve_masked_secrets
from primer.model.provider import HttpConfig, Toolset, ToolsetProviderType, TransportType


def check_python_toolset(entity: Toolset) -> None:
    """Reject a python toolset whose source cannot produce tools.

    The error carries ``field`` and ``lineno`` so a console can point at the offending line.
    """
    from primer.toolset.python_runner.registration import RegistrationError, register_module

    config = entity.config
    try:
        register_module(config.source, entity.id, config.default_timeout_seconds)
    except RegistrationError as exc:
        raise EntityCheckError(
            "validation", str(exc), code="invalid_python_toolset", field=exc.field, extra={"lineno": exc.lineno},
        ) from exc


def own_python_source_version(entity: Toolset, existing: Toolset | None) -> None:
    """Make ``source_version`` server-owned on an update (mutates ``entity``).

    The client's version is advisory. If two operators edit concurrently they both send the version they read, and a parked resume
    could not tell which code it was about to run. The server bumps instead, so the number always moves when the source does, and
    stays put when it does not.
    """
    prior = existing.config if existing is not None else None
    if prior is not None and getattr(prior, "source", None) == entity.config.source:
        entity.config.source_version = prior.source_version
    elif prior is not None:
        entity.config.source_version = prior.source_version + 1


def launches_a_command(entity: Toolset) -> bool:
    """True for an MCP toolset on the ``stdio`` transport: the primer process spawns its ``command`` on the server host."""
    config = entity.config
    return entity.provider == ToolsetProviderType.MCP and getattr(config, "transport", None) == TransportType.STDIO


def runs_python(entity: Toolset) -> bool:
    """True for a python toolset: its source runs on the server host (the local runner is a child of the API / worker process)."""
    return entity.provider == ToolsetProviderType.PYTHON


def _endpoint(entity: Toolset) -> tuple[str, str | None, str | None] | None:
    """Where an http / sse MCP toolset sends its stored secrets: the URL and the OAuth endpoints. ``None`` for any other toolset."""
    config = getattr(entity.config, "config", None)
    if entity.provider != ToolsetProviderType.MCP or not isinstance(config, HttpConfig):
        return None
    oauth = config.oauth
    if oauth is None:
        return (config.url, None, None)
    return (config.url, str(oauth.redirect_uri), oauth.resource_uri)


def repoints_stored_secrets(entity: Toolset, existing: Toolset) -> bool:
    """True when an update changes a toolset's endpoint while a secret it sends back masked would be restored from ``existing``.

    :func:`~primer.model.common.preserve_masked_secrets` swaps a served mask back for the stored value, so without this check a
    caller who never saw the headers / OAuth client secret could move them to a server of its choosing by editing only the URL.
    """
    if _endpoint(entity) == _endpoint(existing):
        return False
    restored = entity.model_copy(deep=True)
    preserve_masked_secrets(restored, existing)
    return restored != entity


_STDIO_REASON = (
    "Creating or changing an MCP toolset on the stdio transport requires the admin role: it launches a command on the server host. "
    "Use an http or sse MCP toolset, or ask an admin."
)
_PYTHON_REASON = (
    "Creating or changing a python toolset requires the admin role: its source runs on the server host. Ask an admin."
)
_REPOINT_REASON = (
    "Changing a toolset's URL or OAuth endpoints while keeping its stored secrets requires the admin role: re-enter the secrets "
    "(headers, OAuth client secret) when changing the URL, or ask an admin."
)


def toolset_admin_reason(entity: Toolset, existing: Toolset | None = None) -> str | None:
    """Why writing ``entity`` (over ``existing``, on an update) is reserved to an admin, or ``None`` when any caller may write it.

    A stdio toolset runs a command of the caller's choosing on the server host the first time it is probed or called, and a python
    toolset runs its source there; both are system configuration, not authoring (provider rows are admin-only for the same reason).
    Either side counts: a user may not turn an http toolset into a stdio or python one, nor edit one they did not create (even to
    point it at http). An update that changes the URL or the OAuth endpoints while a secret is sent back masked would carry the
    stored secret to the new endpoint, so it is admin-only too; re-entering the secrets lifts it. Every other write stays
    user-tier, and a delete launches nothing, so it is not gated.
    """
    if launches_a_command(entity) or (existing is not None and launches_a_command(existing)):
        return _STDIO_REASON
    if runs_python(entity) or (existing is not None and runs_python(existing)):
        return _PYTHON_REASON
    if existing is not None and repoints_stored_secrets(entity, existing):
        return _REPOINT_REASON
    return None


def check_toolset_on_create(entity: Toolset) -> None:
    """The create-time checks a tool can run: a python toolset's source must register."""
    if entity.provider == ToolsetProviderType.PYTHON:
        check_python_toolset(entity)


def check_toolset_on_update(entity: Toolset, existing: Toolset) -> None:
    """The update-time checks, in the router's order: the source must register, then ``source_version`` is set by the server."""
    if entity.provider != ToolsetProviderType.PYTHON:
        return
    check_python_toolset(entity)
    own_python_source_version(entity, existing)


__all__ = [
    "check_python_toolset",
    "check_toolset_on_create",
    "check_toolset_on_update",
    "launches_a_command",
    "own_python_source_version",
    "repoints_stored_secrets",
    "runs_python",
    "toolset_admin_reason",
]
