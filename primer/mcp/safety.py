"""``is_exposable`` predicate for the MCP server endpoint.

Spec §7 (revised). The MCP server endpoint exposes a curated subset
of primer's internal tools to external clients. Operators manage the
allowlist from the console; this module enforces ONLY the technical
constraints that v1 MCP can't represent:

* yielding tools — the agent runtime parks them on an event bus,
  MCP v1 tools/list has no equivalent pause/resume primitive.
* workspace tools requiring an ``AgentSession`` — they read
  ``ctx.session_id``, which is meaningless outside an agent loop.

There is intentionally no policy-level denylist. The operator
chose to enable MCP, chose which tools to expose, and authenticated
the caller with a bearer token they minted themselves. They get to
choose the risk. (Earlier versions of this module hard-denied
``system__call_tool`` (meta-dispatcher) and ``web__http_request``
(SSRF surface); both were paternalistic and have been removed —
operators can opt in to either if they understand the implications.)

Public surface:

* :func:`is_exposable` — predicate the exposure service consults
  before adding a tool to the live allowlist (and the dispatch path
  re-runs on every call as defence-in-depth).
* :func:`tool_scoped_id` — helper to compute the wire-level
  ``toolset_id__tool_id`` identifier from a :class:`Tool`.
* :data:`HARD_DENY` — retained as an empty frozenset for
  back-compatibility with callers that imported the name. New
  code should not rely on it.
"""

from __future__ import annotations

from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.int.toolset import ToolsetProvider
from primer.model.chat import Tool


HARD_DENY: frozenset[str] = frozenset()
"""Empty — kept as a stable import name. See module docstring."""


def tool_scoped_id(tool: Tool) -> str:
    """Return the wire-level ``toolset_id__tool_id`` identifier.

    The MCP server publishes tools under this scoped form so that
    name collisions between toolsets are impossible (two providers
    each exposing ``read_file`` get distinct ``workspaces__read_file``
    / ``foo__read_file`` ids).
    """
    return f"{tool.toolset_id}__{tool.id}"


def is_exposable(
    tool: Tool,
    *,
    provider: ToolsetProvider,
) -> tuple[bool, str | None]:
    """Return ``(ok, reason_if_denied)`` for exposing ``tool`` over MCP.

    The check is intentionally cheap and synchronous — it runs on
    every PUT validation pass and inside the per-request dispatch
    filter. The denial reasons (returned only when ``ok`` is False)
    are stable strings the UI surfaces to operators:

    * ``"yielding_unsupported"`` — handler yields; MCP v1 has no
      park/resume protocol so the round-trip is impossible.
    * ``"needs_session"`` — workspace tool that requires a live
      :class:`AgentSession` (reads ``ctx.session_id``); meaningless
      outside an agent loop.

    Approval-gated tools are NOT filtered here — that check lives in
    the dispatcher layer (Phase 4) where the
    :class:`ApprovalResolver` is wired up. Keeping ``is_exposable``
    free of async dependencies lets the exposure service and the
    REST validator share the same predicate.

    System-only floor: the MCP endpoint exists to expose the platform's
    own capabilities (the reserved built-in toolsets) to external
    agents. Tools from user-defined Toolset rows are never exposable,
    regardless of the operator allowlist; they belong to the platform's
    internal agents, not to outside MCP clients.
    """
    if tool.toolset_id not in RESERVED_TOOLSET_IDS:
        return False, "not_system_toolset"
    if provider.is_yielding(tool.id):
        return False, "yielding_unsupported"
    if tool.toolset_id == "workspaces" and provider.requires_session(tool.id):
        return False, "needs_session"
    return True, None


__all__ = ["HARD_DENY", "is_exposable", "tool_scoped_id"]
