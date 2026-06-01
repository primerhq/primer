"""Hard denylist + ``is_exposable`` predicate for the MCP server endpoint.

Spec §7. The MCP server endpoint exposes a curated subset of primer's
internal tools to external clients. Operators manage the allowlist
from the console; THIS module enforces the floor — denials that
operators cannot override because they guard against privilege
escalation or known protocol mismatches.

Public surface:

* :data:`HARD_DENY` — frozenset of ``toolset_id__tool_id`` strings
  that the endpoint refuses to expose under any circumstance.
* :func:`is_exposable` — predicate the exposure service consults
  before adding a tool to the live allowlist (and the dispatch path
  re-runs on every call as defence-in-depth).
* :func:`tool_scoped_id` — helper to compute the wire-level
  ``toolset_id__tool_id`` identifier from a :class:`Tool`.
"""

from __future__ import annotations

from primer.int.toolset import ToolsetProvider
from primer.model.chat import Tool


HARD_DENY: frozenset[str] = frozenset({
    # Meta-dispatcher: invoking it executes an arbitrary other tool,
    # bypassing the MCP allowlist entirely. Exposing it would defeat
    # the entire safety model.
    "system__call_tool",
    # SSRF surface: lets a remote MCP client make arbitrary outbound
    # HTTP requests from primer's process. Until we have a network
    # allowlist, this stays denied at the floor.
    "web__http-request",
})
"""Tools that operators cannot expose over MCP, no matter the UI state.

Membership is checked against the scoped ``toolset_id__tool_id`` form
(see :func:`tool_scoped_id`). The set is intentionally tiny — broad
allowlist-by-policy lives in the operator-managed
:class:`primer.model.mcp_exposure.McpExposure` row; this floor only
catches the categorically-unsafe primitives.
"""


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

    * ``"hard_denied"`` — in :data:`HARD_DENY`.
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
    """
    scoped = tool_scoped_id(tool)
    if scoped in HARD_DENY:
        return False, "hard_denied"
    if provider.is_yielding(tool.id):
        return False, "yielding_unsupported"
    if tool.toolset_id == "workspaces" and provider.requires_session(tool.id):
        return False, "needs_session"
    return True, None


__all__ = ["HARD_DENY", "is_exposable", "tool_scoped_id"]
