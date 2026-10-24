"""Unified reply binding: where a session's outbound traffic replies to.

A "reply binding" answers one question: when a workspace session emits
something the operator should see (a gate forward, an ``inform`` message,
the final result; there is no start acknowledgement), which channel (and
optional anchor) does it post to?

Two scopes resolve through :func:`resolve_reply_binding`, most-specific
first:

* a per-session ephemeral binding stored under
  :data:`SESSION_REPLY_BINDING_KEY` in ``WorkspaceSession.metadata`` (set
  by the inbound router when a channel event spawns the session); and
* the workspace-standing :attr:`Workspace.reply_binding` link.

When neither resolves the session is silent (non-channel session).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from primer.model.trigger import ExplicitReplyTarget, ReplyTarget, ReplyTargetInput

_log = logging.getLogger(__name__)


SESSION_REPLY_BINDING_KEY = "reply_binding"
"""``WorkspaceSession.metadata`` key holding the ephemeral binding dict."""


class ReplyBinding(BaseModel):
    """A resolved outbound reply destination: a channel plus optional anchor."""

    channel_id: str = Field(
        ..., description="Channel this session's outbound traffic replies to."
    )
    anchor: str | None = Field(
        default=None,
        description=(
            "Optional thread inside the channel (a Slack thread ts, a Discord "
            "thread id) that every post goes into. None: the adapter opens or "
            "reuses the session's own thread (Slack, Discord) or posts into "
            "the room (Telegram, which ignores an anchor)."
        ),
    )
    quiet: bool = Field(
        default=False,
        description=(
            "Per-binding quiet mode: when True the lifecycle relay suppresses "
            "the final result of each turn for this session (questions, "
            "approval requests and inform updates still post)."
        ),
    )
    silent: bool = Field(
        default=False,
        description=(
            "When True nothing is posted at all: no question, approval "
            "request, update or answer (a binding's reply_target 'none'). "
            "The session's questions are answered in the console. A silent "
            "binding is quiet too."
        ),
    )


async def resolve_reply_binding(
    session,
    *,
    storage_provider,
) -> ReplyBinding | None:
    """Resolve the outbound reply binding for ``session``.

    Precedence: the session-ephemeral binding in
    ``session.metadata[SESSION_REPLY_BINDING_KEY]`` wins, else the
    workspace-standing :attr:`Workspace.reply_binding`, else None. The
    workspace load is guarded so a storage error never raises into the
    dispatcher.
    """
    from primer.observability import metrics

    meta = getattr(session, "metadata", None) or {}
    session_binding = meta.get(SESSION_REPLY_BINDING_KEY)
    if isinstance(session_binding, dict) and session_binding.get("channel_id"):
        metrics.reply_binding_resolutions_total.labels(scope="session").inc()
        return ReplyBinding.model_validate(session_binding)

    try:
        from primer.model.workspace import Workspace

        ws = await storage_provider.get_storage(Workspace).get(
            session.workspace_id
        )
    except Exception as exc:  # never raise into the dispatcher
        _log.warning("resolve_reply_binding: workspace load failed: %s", exc)
        metrics.reply_binding_resolutions_total.labels(scope="none").inc()
        return None
    if ws is not None and ws.reply_binding is not None:
        metrics.reply_binding_resolutions_total.labels(scope="workspace").inc()
        # The anchor comes with it: the dispatcher puts it on every post, so a workspace bound with an anchor posts into that
        # thread (ticket 01a16e37-b98e item 5; it used to be dropped here and every session opened its own thread).
        return ReplyBinding(channel_id=ws.reply_binding.channel_id, anchor=ws.reply_binding.anchor)
    metrics.reply_binding_resolutions_total.labels(scope="none").inc()
    return None


__all__ = [
    "ExplicitReplyTarget",
    "ReplyBinding",
    "ReplyTarget",
    "ReplyTargetInput",
    "SESSION_REPLY_BINDING_KEY",
    "resolve_reply_binding",
]
