"""Pre-write checks for :class:`~primer.model.provider.Toolset`, shared by the REST router and the system tools (task 01a111d1, D5
phase 2b).

Moved out of ``primer/api/routers/providers.py``: a python toolset's source must register (registration is where a bad tool must
fail; calling time is too late, an agent mid-turn should never be the thing that discovers a docstring is missing), and its
``source_version`` is SERVER-owned. The router's hooks re-raise the exact ``HTTPException`` they always raised; the system
``create_`` / ``update_toolset`` tools answer ``validation-error``.

Deliberately NOT here: the router's reachability probe for http / sse MCP toolsets (an 8 s outbound call, bypassed by
``?allow_unreachable``). A tool runs inside an agent turn, so it does not probe; the difference is documented in rest-api.md and in
the tool descriptions, and pinned by a test.
"""

from __future__ import annotations

from primer.common.entity_checks import EntityCheckError
from primer.model.provider import Toolset, ToolsetProviderType


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
    "own_python_source_version",
]
