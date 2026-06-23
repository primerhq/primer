"""Unified reply binding: where a session's outbound traffic replies to.

A "reply binding" answers one question: when a workspace session emits
something the operator should see (a gate forward, an ``inform`` message,
a start ack, the final result), which channel (and optional anchor) does
it post to?

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
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

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
            "Optional thread/message anchor inside the channel (e.g. a Slack "
            "thread ts) the reply attaches to. None posts to the channel root."
        ),
    )
    quiet: bool = Field(
        default=False,
        description=(
            "Per-binding quiet mode: when True the lifecycle relay suppresses "
            "the start ack and the final result for this session (gates still "
            "forward). Per spec 8 this is per-binding overridable."
        ),
    )


class _ExplicitReplyTarget(BaseModel):
    """An explicit reply target naming the channel (and optional anchor)."""

    channel_id: str = Field(
        ..., description="Channel the reply is pinned to."
    )
    anchor: str | None = Field(
        default=None,
        description="Optional thread/message anchor inside the channel.",
    )
    quiet: bool = Field(
        default=False,
        description=(
            "Per-binding quiet mode: suppress the lifecycle relay (start ack "
            "and final result) for this target."
        ),
    )


ReplyTarget = Annotated[
    Literal["source_thread", "source_room", "dm_sender", "none"] | _ExplicitReplyTarget,
    Field(
        description=(
            "Where an action's outbound reply goes. One of the relative "
            "literals (source_thread / source_room / dm_sender / none) "
            "resolved against the triggering event, or an explicit "
            "{channel_id, anchor} target."
        ),
    ),
]
"""Discriminated reply-target value carried on a Subscription."""


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
        return ReplyBinding(channel_id=ws.reply_binding.channel_id)
    metrics.reply_binding_resolutions_total.labels(scope="none").inc()
    return None


__all__ = [
    "ReplyBinding",
    "ReplyTarget",
    "SESSION_REPLY_BINDING_KEY",
    "resolve_reply_binding",
]
