"""Registry mapping yielding tool names to their resume hooks.

A yielding tool's tool-call lifecycle is split across two functions:

* The handler that returns :class:`Yielded` (called when the agent
  invokes the tool).
* The ``resume`` hook that converts the eventual event payload into
  the tool result the LLM sees (called when the parked session
  becomes resumable).

The two functions live next to each other in the tool's module
(``primer.toolset.misc.sleep_resume`` for the prototype). The
worker's resume path needs to find the right hook by tool name —
this module owns that lookup table.

Resume hooks are registered eagerly at import time. The tool's
module imports this registry and calls :func:`register_resume_hook`
at module load. The registry is a module-global dict (fine — there's
exactly one in-process worker pool per server) and is read-only from
the worker pool's perspective.

Future yielding tools (ask_user, watch_files, MCP Tasks) register
the same way. Tools that don't yield don't appear here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Union

from primer.model.chat import ToolCallResult
from primer.model.except_ import ConfigError
from primer.model.yield_ import YieldCancelled, YieldTimeout


# Signature of a tool's resume hook.
# Receives:
#   * yield_metadata — the dict the tool stamped into Yielded.resume_metadata
#     at park time, augmented by the worker with parked_at_iso (so the
#     hook can compute elapsed_seconds).
#   * event_payload  — either a real event dict, or a YieldTimeout, or a
#     YieldCancelled instance.
# Returns the ToolCallResult the LLM will see as the tool's response.
ResumeHook = Callable[
    [dict[str, Any], Union[dict[str, Any], YieldTimeout, YieldCancelled]],
    Union[ToolCallResult, Awaitable[ToolCallResult]],
]


_registry: dict[str, ResumeHook] = {}


def register_resume_hook(tool_name: str, hook: ResumeHook) -> None:
    """Register the resume hook for a yielding tool.

    Idempotent on the (tool_name, hook) pair — re-registering the
    same hook is fine. Re-registering a different hook for the same
    tool name raises :class:`ConfigError` (the second registration
    means a bug, not a deliberate override).
    """
    existing = _registry.get(tool_name)
    if existing is not None and existing is not hook:
        raise ConfigError(
            f"resume hook for tool {tool_name!r} already registered "
            f"as {existing!r}; refusing to overwrite with {hook!r}"
        )
    _registry[tool_name] = hook


def get_resume_hook(tool_name: str) -> ResumeHook:
    """Look up the resume hook for a yielding tool.

    Raises :class:`ConfigError` if no hook is registered — that
    indicates the parked-state blob references a tool whose
    registration was lost (e.g. tool removed from the codebase
    between park and resume). The worker should treat this as a
    fatal resume failure.
    """
    hook = _registry.get(tool_name)
    if hook is None:
        raise ConfigError(
            f"no resume hook registered for yielding tool "
            f"{tool_name!r}; the tool may have been removed from "
            f"this build between park and resume"
        )
    return hook


def has_resume_hook(tool_name: str) -> bool:
    """Cheap presence check without raising."""
    return tool_name in _registry


def _reset_for_tests() -> None:
    """Test-only: wipe the registry between tests.

    Used by unit tests that exercise the registry's idempotency /
    conflict semantics in isolation. Production code never calls
    this.
    """
    _registry.clear()


__all__ = [
    "ResumeHook",
    "get_resume_hook",
    "has_resume_hook",
    "register_resume_hook",
]
