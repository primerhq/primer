"""The inner dispatch: one tool running another tool of any toolset for the same run.

``system__call_tool`` runs a tool by ``(toolset_id, tool_name)``, and the approval re-runs of such a call run it again. The tool manager's
RBAC floor (``ToolExecutionManager._dispatch_toolset``) only ever sees the OUTER tool, so whatever runs a tool for another must check the
INNER tool's ``required_role`` itself, or a user-role run reaches every admin tool through a role-user door. This module is that check and
that dispatch, in one place:

* :func:`run_principal` is the principal the run is ranked as: the context's ``initiated_by``, else
  :meth:`PrincipalRef.unattributed` (an ordinary ``user``). A call with no context (a bare tool manager, ``invoke_agent`` over MCP) or a
  context that names no initiator is an unknown caller, and an unknown caller never clears an admin floor (security review A-20).
* :func:`inner_role_refusal` is the ``forbidden`` refusal when that principal does not rank at or above the inner tool's role, under the
  floor's own predicate (:func:`primer.authz._role_allows`).
* :func:`dispatch_inner_tool` refuses, or calls the inner provider with the caller's OWN :class:`ToolContext`, unchanged, and the principal
  string that context carries (:attr:`ToolContext.principal`, set by the dispatch layer), never one a model chose.

It imports nothing from ``primer.toolset`` (which imports this package), so it builds the error envelope itself: the
``{"type", "message"}`` shape of ``primer.toolset._helpers.err``.
"""

from __future__ import annotations

import json
from typing import Any

from primer.authz import _role_allows
from primer.int.toolset import ToolsetProvider
from primer.model.chat import ToolCallResult
from primer.model.principal import PrincipalRef
from primer.model.yield_ import ToolContext


#: The error ``type`` of a role refusal.
FORBIDDEN_ERROR_TYPE = "forbidden"


def run_principal(ctx: ToolContext | None) -> PrincipalRef:
    """The principal the run is ranked as: ``ctx.initiated_by`` when set, else :meth:`PrincipalRef.unattributed` (an ordinary user)."""
    if ctx is not None and ctx.initiated_by is not None:
        return ctx.initiated_by
    return PrincipalRef.unattributed()


def inner_role_refusal(
    provider: ToolsetProvider, *, toolset_id: str, tool_name: str, ctx: ToolContext | None,
) -> ToolCallResult | None:
    """``None`` when the run's principal may run ``tool_name`` of ``provider``, else the ``forbidden`` refusal to return instead of running it."""
    principal = run_principal(ctx)
    need = provider.required_role(tool_name)
    if _role_allows(principal, need):
        return None
    have = principal.role or principal.type
    return ToolCallResult(
        output=json.dumps({
            "type": FORBIDDEN_ERROR_TYPE,
            "message": (
                f"tool {tool_name!r} in toolset {toolset_id!r} requires the {need!r} role; "
                f"this run's principal has the {have!r} role"
            ),
        }),
        is_error=True,
    )


async def dispatch_inner_tool(
    *,
    provider: ToolsetProvider,
    toolset_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    ctx: ToolContext | None,
) -> ToolCallResult:
    """Run ``tool_name`` of ``provider`` for the run that ``ctx`` belongs to: the role refusal when there is one, else the inner result.

    The inner provider gets ``ctx`` itself (so a pausing tool parks under the caller's call id, and an owner-dependent tool sees the run's
    identity) and ``ctx.principal`` as its principal. A :class:`primer.model.yield_.YieldToWorker` (the inner tool paused) and a
    :class:`primer.model.except_.PrimerError` propagate to the caller, which decides how to report them.
    """
    refusal = inner_role_refusal(provider, toolset_id=toolset_id, tool_name=tool_name, ctx=ctx)
    if refusal is not None:
        return refusal
    return await provider.call(
        tool_name=tool_name,
        arguments=arguments,
        principal=ctx.principal if ctx is not None else None,
        ctx=ctx,
    )


__all__ = ["FORBIDDEN_ERROR_TYPE", "dispatch_inner_tool", "inner_role_refusal", "run_principal"]
