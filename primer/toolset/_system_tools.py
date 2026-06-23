"""Hand-written individual system tools (non-CRUD) for the ``system`` toolset.

Split out of :mod:`primer.toolset.system` (a god-module decomposition). This
module holds the bespoke, hand-written tools whose handlers do not come from
the generic CRUD factory. Today that is the ``ask_user`` yielding tool (its
argument model, resume hook, and handler); ``build_system_toolset`` in
``system.py`` wires the tool entry itself (the other bespoke tools -
reply-binding, channel-binding, invoke_agent, switch_to_agent - are defined
inline in the builder because they close over its per-build dependencies).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError

from primer.model.chat import ToolCallResult
from primer.toolset._helpers import err as _err, ok as _ok
from primer.model.yield_ import ToolContext, Yielded
from primer.toolset._system_common import _err_from_validation


# ===========================================================================
# ask_user - yielding tool. Lives in the ``system`` toolset (alongside
# switch_to_agent) so it is available everywhere, including chats. It
# soft-yields in chats (degrades to a conversational turn keyed on the
# bare name ``ask_user``) and parks in workspace sessions.
#
# Pauses the agent's turn until a human operator types a response via
# the API surface (GET .../ask_user/pending + POST .../ask_user/respond).
# The optional ``timeout_seconds`` falls back to the global yield cap
# when omitted. The optional ``response_schema`` is surfaced to the
# UI and validated server-side at POST time.
# ===========================================================================


class _AskUserArgs(BaseModel):
    """Prompt the operator sees and shape of the expected reply."""

    prompt: str = Field(
        ...,
        min_length=1,
        max_length=8000,
        description=(
            "Question or instruction shown to the operator. Newlines "
            "are preserved by the UI panel. Required."
        ),
    )
    response_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional JSON Schema the operator's response must satisfy. "
            "Validated server-side at POST time; a violation is "
            "surfaced inline in the UI without resuming the agent. "
            "Omit for free-text responses."
        ),
    )
    timeout_seconds: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Optional per-call timeout. When omitted, falls back to "
            "the global yield cap (default 60 minutes). If the "
            "operator doesn't respond in time the resume hook returns "
            "``{timed_out: true, elapsed_seconds: ...}`` so the agent "
            "can decide whether to retry or proceed."
        ),
    )
    files: list[str] | None = Field(
        default=None,
        description=(
            "Optional workspace-relative file paths to attach to the prompt. "
            "Each file is read from the session's workspace, stored, and sent "
            "to the channel as media (image/document/audio) alongside the "
            "prompt text. Ignored on the chat surface (no workspace)."
        ),
    )


def ask_user_resume(
    yield_metadata: dict[str, Any],
    event_payload: Any,
) -> ToolCallResult:
    """Resume hook for ask_user - translate payload into tool result.

    Three branches:

    * real response (``{"response": <any>}`` from the POST endpoint) →
      ``{"response": <any>}``
    * :class:`YieldTimeout` from the sweeper → ``{"timed_out": true,
      "elapsed_seconds": ...}``
    * :class:`YieldCancelled` from the cancel-yielded-tool API →
      ``{"cancelled": true, "reason": ..., "elapsed_seconds": ...}``

    ``yield_metadata`` carries ``parked_at_iso`` (worker-injected) so
    we can compute elapsed even if the event payload didn't include
    it (defensive - both timeout and cancel synthesise elapsed
    upstream via :func:`classify_resume_payload`, but the dataclass
    instance is the source of truth).
    """
    from primer.model.yield_ import YieldCancelled, YieldTimeout  # avoid cycle

    if isinstance(event_payload, YieldTimeout):
        return _ok(
            {
                "timed_out": True,
                "elapsed_seconds": event_payload.elapsed_seconds,
            }
        )
    if isinstance(event_payload, YieldCancelled):
        return _ok(
            {
                "cancelled": True,
                "reason": event_payload.reason,
                "elapsed_seconds": event_payload.elapsed_seconds,
            }
        )
    # Real operator response from the POST endpoint.
    response = (
        event_payload.get("response")
        if isinstance(event_payload, dict)
        else None
    )
    return _ok({"response": response})


async def _ask_user_handler(
    arguments: dict[str, Any],
    *,
    ctx: ToolContext,
) -> ToolCallResult | Yielded:
    try:
        args = _AskUserArgs.model_validate(arguments)
    except ValidationError as exc:
        return _err_from_validation(exc)

    # Scope the event_key on (session_id|chat_id, tool_call_id). The session
    # path (workspace sessions) uses session_id and PARKS; a chat has no
    # session, so fall back to chat_id (the chat surface degrades a yield to a
    # conversational turn rather than parking). Fail only when neither id exists.
    scope_id = ctx.session_id or ctx.chat_id
    if scope_id is None:
        return _err(
            "ask_user requires ctx.session_id or ctx.chat_id; the worker must "
            "pass the live session or chat id when invoking yielding tools",
            error_type="bad-request",
        )

    return Yielded(
        tool_name="",  # filled in by the provider
        event_key=f"ask_user:{scope_id}:{ctx.tool_call_id}",
        timeout=args.timeout_seconds,
        resume_metadata={
            "prompt": args.prompt,
            "response_schema": args.response_schema,
            "tool_call_id": ctx.tool_call_id,
            "files": args.files or None,
        },
    )
