"""Invoker-supplied (external) tools: provider, park raise, resume hook.

An API caller may attach tool definitions to an invocation (session
steer / chat send). Those defs become an in-memory toolset merged into
the turn's :class:`~primer.agent.tool_manager.ToolExecutionManager`
catalogue under the reserved ``external`` toolset id. Calling one never
executes anything server-side: the provider records a pending
:class:`~primer.model.external_tool.ExternalToolCall` row (the
API-facing record) and raises :class:`YieldToWorker` so the turn parks
until the invoker responds through the invocation API.

The park marker tool name is ``_external`` (mirroring the approval
gate's ``_approval``): the real tool name is dynamic and cannot key the
resume registry, so the marker does, with the original call carried in
``resume_metadata``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from primer.int.toolset import ToolsetProvider
from primer.model.chat import Tool, ToolCallResult
from primer.model.external_tool import ExternalToolCall, ExternalToolDef
from primer.model.yield_ import (
    ToolContext,
    Yielded,
    YieldCancelled,
    YieldTimeout,
    YieldToWorker,
)
from primer.worker.yield_resume_registry import (
    ResumeContext,
    register_resume_hook,
)

logger = logging.getLogger(__name__)

EXTERNAL_TOOLSET_ID = "external"
EXTERNAL_PARK_TOOL_NAME = "_external"


def external_event_key(owner_id: str, tool_call_id: str) -> str:
    """Event key for one external tool call park.

    ``owner_id`` is the session id (session/graph surfaces) or the chat
    id (chat surface) - whichever conversation owns the turn.
    """
    return f"external_tool:{owner_id}:{tool_call_id}"


class ExternalToolsetProvider(ToolsetProvider):
    """In-memory ToolsetProvider over one invocation's external tool defs.

    ``call`` NEVER returns a result: it records the pending
    ``ExternalToolCall`` row, then raises ``YieldToWorker`` so the turn
    parks until the invoker responds through the invocation API.

    ``node_id`` (graph surfaces) stamps which node raised each call so
    the pending endpoints can attribute concurrent parks.
    """

    def __init__(
        self,
        *,
        defs: list[ExternalToolDef],
        call_storage: Any,
        node_id: str | None = None,
    ) -> None:
        self._defs = {d.name: d for d in defs}
        self._storage = call_storage
        self._node_id = node_id

    async def list_tools(
        self,
        *,
        principal: str | None = None,
    ) -> AsyncIterator[Tool]:
        del principal  # external tools are invocation-scoped, not per-user
        for d in self._defs.values():
            yield Tool(
                id=d.name,
                description=d.description,
                toolset_id=EXTERNAL_TOOLSET_ID,
                args_schema=d.args_schema,
                yields=True,
            )

    def is_yielding(self, tool_name: str) -> bool:
        del tool_name
        return True

    def required_role(self, tool_name: str) -> str:
        # External tools are caller-mediated: the invocation endpoint
        # already gated on the agent's allow_external_tools flag, and the
        # "execution" is the invoker answering its own tool. Any
        # authenticated invoker rank passes.
        del tool_name
        return "user"

    async def call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        principal: str | None = None,
        ctx: ToolContext | None = None,
    ) -> ToolCallResult:
        del principal
        d = self._defs.get(tool_name)
        if d is None:
            # Routing-table membership guarantees this never fires; keep
            # the standard in-band error shape for safety.
            return ToolCallResult(
                output=f"unknown external tool {tool_name!r}", is_error=True
            )
        if ctx is None:
            # Yielding requires a tool_call_id to form the event key; a
            # ctx-less dispatch is a programming error, not a runtime
            # condition.
            return ToolCallResult(
                output=(
                    "external tools require dispatch context (tool_call_id); "
                    "manager did not supply ToolContext"
                ),
                is_error=True,
            )
        owner_id = ctx.session_id or ctx.chat_id or "unknown"
        now = datetime.now(UTC)
        row = ExternalToolCall(
            session_id=ctx.session_id,
            chat_id=ctx.chat_id,
            node_id=self._node_id,
            tool_call_id=ctx.tool_call_id,
            tool_name=tool_name,
            arguments=dict(arguments or {}),
            created_at=now,
            timeout_at=(
                now + timedelta(seconds=d.timeout_seconds)
                if d.timeout_seconds
                else None
            ),
        )
        try:
            await self._storage.create(row)
        except Exception:  # noqa: BLE001 - the park must not be lost
            logger.exception(
                "external tool call row write failed for %s", ctx.tool_call_id
            )
        raise YieldToWorker(
            Yielded(
                tool_name=EXTERNAL_PARK_TOOL_NAME,
                event_key=external_event_key(owner_id, ctx.tool_call_id),
                timeout=d.timeout_seconds,
                resume_metadata={
                    "original_call": {
                        "id": ctx.tool_call_id,
                        "name": tool_name,
                        "arguments": dict(arguments or {}),
                    },
                    "external_call_row_id": row.id,
                },
            ),
            tool_call_id=ctx.tool_call_id,
        )


def _result_output(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)


def external_resume_hook(
    yield_metadata: dict[str, Any],
    event_payload: Any,
    ctx: ResumeContext,
) -> ToolCallResult:
    """Translate a resume payload into the external call's tool result.

    Registered under ``_external`` in the worker's resume registry.
    Payload shapes: ``{"result": Any, "is_error": bool}`` published by
    the invocation endpoint, or the synthetic :class:`YieldTimeout` /
    :class:`YieldCancelled` sentinels from the sweeper / cancel API.
    """
    del yield_metadata, ctx
    if isinstance(event_payload, YieldTimeout):
        return ToolCallResult(
            output=json.dumps({"timed_out": True}), is_error=True
        )
    if isinstance(event_payload, YieldCancelled):
        return ToolCallResult(
            output=json.dumps(
                {
                    "cancelled": True,
                    "reason": event_payload.reason
                    or "superseded by new user message",
                }
            ),
            is_error=True,
        )
    data = (
        event_payload
        if isinstance(event_payload, dict)
        else {"result": event_payload}
    )
    return ToolCallResult(
        output=_result_output(data.get("result")),
        is_error=bool(data.get("is_error", False)),
    )


# Register at import time, like the other yielding tools (see
# primer/toolset/system.py for ask_user). primer/worker/
# session_resume_coordinator.py imports this module explicitly so the
# hook exists in a worker process that resumes a park it never created
# (e.g. after a restart).
register_resume_hook(EXTERNAL_PARK_TOOL_NAME, external_resume_hook)


__all__ = [
    "EXTERNAL_PARK_TOOL_NAME",
    "EXTERNAL_TOOLSET_ID",
    "ExternalToolsetProvider",
    "external_event_key",
    "external_resume_hook",
]
