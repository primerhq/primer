"""Channel adapter ABC + provider-agnostic envelope types."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from primer.model.channel import ChannelProviderType

# Channels that anchor one thread per chat (multi-type). Telegram has no
# threads (single-type: one 1:1 chat per channel).
_THREADED_PROVIDERS = frozenset({
    ChannelProviderType.SLACK, ChannelProviderType.DISCORD,
})

# Default cap for per-adapter correlation maps (in-flight prompt -> ids). Sized
# for a busy bot's recent prompts; older entries evict (their parks, if still
# open, fall back to the durable CorrelationStore / self-describing button
# payloads on resume). Hoisted here so every adapter bounds its caches the same
# way instead of growing them without limit for the life of the process.
DEFAULT_CACHE_MAXSIZE = 10_000


class BoundedDict(OrderedDict):
    """An insertion-ordered dict that evicts the oldest entry once it exceeds
    ``maxsize``. Re-inserting an existing key refreshes its recency
    (move-to-end), so the LRU victim is always the least-recently-written key.

    Used by every channel adapter to bound its session->thread / tag->ids
    correlation maps so a long-lived bot does not leak memory.
    """

    def __init__(self, *, maxsize: int = DEFAULT_CACHE_MAXSIZE) -> None:
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key, value) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self._maxsize:
            self.popitem(last=False)


def provider_supports_threads(provider_type: ChannelProviderType) -> bool:
    """True for multi-type channels (Slack/Discord), False for Telegram."""
    return provider_type in _THREADED_PROVIDERS


def session_thread_label(session_id: str) -> str:
    """Human-facing title for a per-session conversation thread.

    Channels that support threads (Slack, Discord) anchor one thread per agent
    session and route every prompt (ask_user + tool approvals) into it.
    """
    return f"Agent session {session_id}"


def format_tool_args(tool_args: dict[str, Any] | None) -> str:
    """Pretty-print tool-call arguments as JSON for channel rendering.

    Channels show this inside a code block instead of dumping the raw
    ``repr`` of the dict. Falls back to ``str`` if the args are not
    JSON-serialisable.
    """
    if not tool_args:
        return "{}"
    try:
        return json.dumps(tool_args, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(tool_args)


def attribution_header(env: "PromptEnvelope") -> str:
    """Return a one-line attribution prefix for gate posts.

    Returns an empty string when neither workspace_name nor session_label is
    set so callers can unconditionally prepend without adding blank lines to
    messages that carry no attribution context.
    """
    if not (env.workspace_name or env.session_label):
        return ""
    ws = env.workspace_name or "workspace"
    sess = env.session_label or "session"
    return f"\U0001F6E0 Workspace: {ws} · Session: {sess}\n"


@dataclass
class PromptEnvelope:
    """Provider-agnostic ask-user / approval payload."""

    kind: str
    workspace_id: str
    session_id: str
    tool_call_id: str
    prompt: str
    response_schema: dict[str, Any] | None
    choices: list[str] | None
    timeout_at_iso: str | None
    # Structured approval detail (kind == "tool_approval"), so renderers can
    # format the call cleanly instead of parsing it out of ``prompt``.
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    # Artifact-backed media parts (as dicts) to upload alongside the prompt,
    # e.g. workspace files attached to ask_user / inform_user. None == no media.
    media: list[dict[str, Any]] | None = None
    # Optional attribution context surfaced in channel gate posts.
    workspace_name: str | None = None
    session_label: str | None = None


@dataclass
class ResponseEnvelope:
    """Provider-agnostic response from the platform."""

    kind: str
    workspace_id: str
    session_id: str
    tool_call_id: str
    response: Any
    decision: str | None
    reason: str | None
    platform_metadata: dict[str, Any] = field(default_factory=dict)


class ChannelAdapter(ABC):
    """Per-channel adapter instance.

    Subclasses set ``self._channel``, ``self._inbox`` and the optional
    chat-surface wiring (``self._sp`` / ``self._bus`` / ``self._claim_engine``)
    in ``__init__``; the concrete helpers below (inbound routing, gate-decision
    relay) read those attributes, so every adapter shares one implementation.
    """

    # -- attributes the shared helpers below read off the subclass instance --
    _channel: Any
    _inbox: Any
    _sp: Any
    _bus: Any
    _claim_engine: Any

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def aclose(self) -> None: ...

    @abstractmethod
    async def verify(self) -> None:
        """Smoke-test the credential set + target id. Raises on failure."""

    @abstractmethod
    async def post_prompt(
        self, envelope: PromptEnvelope,
    ) -> dict[str, Any]:
        """Render and post the envelope."""

    # -- per-provider hooks ------------------------------------------------

    def _user_id_key(self) -> str:
        """Metadata key under which the acting user's id is recorded on a
        :class:`ResponseEnvelope`. Provider adapters override this with their
        own key (e.g. ``"slack_user_id"``); the base default keeps non-relaying
        adapters (NullChannelAdapter, test doubles) instantiable."""
        return "user_id"

    def _user_id_default(self) -> Any:
        """Value stored under :meth:`_user_id_key` when no user id is known.

        Slack stores ``""`` (ids are strings); Telegram/Discord store ``0``
        (ids are ints). Defaults to ``0``; Slack overrides to ``""``.
        """
        return 0

    # -- shared gate-decision relay ----------------------------------------

    async def _handle_decision(
        self, *,
        workspace_id: str, session_id: str, tool_call_id: str,
        decision: str, reason: str | None,
        user_id: Any = None,
    ) -> None:
        """Relay a tool-approval decision (Approve/Reject) to the inbox.

        The acting user's id is recorded under the provider-specific
        :meth:`_user_id_key` so renderers can attribute the decision.
        """
        await self._inbox.handle_response(ResponseEnvelope(
            kind="tool_approval",
            workspace_id=workspace_id, session_id=session_id,
            tool_call_id=tool_call_id,
            response=None, decision=decision, reason=reason,
            platform_metadata={
                self._user_id_key(): user_id
                if user_id is not None else self._user_id_default(),
            },
        ))

    async def _handle_text_reply(
        self, *,
        workspace_id: str, session_id: str, tool_call_id: str,
        text: str,
        user_id: Any = None,
    ) -> None:
        """Relay a free-text ask_user reply to the inbox."""
        await self._inbox.handle_response(ResponseEnvelope(
            kind="ask_user",
            workspace_id=workspace_id, session_id=session_id,
            tool_call_id=tool_call_id,
            response=text, decision=None, reason=None,
            platform_metadata={
                self._user_id_key(): user_id
                if user_id is not None else self._user_id_default(),
            },
        ))

    # -- shared inbound routing --------------------------------------------

    def _inbound_router(self):
        """Build a :class:`ChannelInboundRouter` from the adapter's wiring, or
        ``None`` when chat-surface dispatch is not configured (no storage
        provider). The ``route``/``route_event`` paths share one router build,
        so :meth:`_event_router` is an alias.
        """
        if self._sp is None:
            return None
        from primer.channel.chat_inbox import ChatResponseInbox
        from primer.channel.correlation import CorrelationStore
        from primer.channel.inbound_router import ChannelInboundRouter
        gate_inbox = ChatResponseInbox(
            storage_provider=self._sp, event_bus=self._bus,
            claim_engine=self._claim_engine)
        return ChannelInboundRouter(
            self._sp, CorrelationStore(self._sp), event_bus=self._bus,
            claim_engine=self._claim_engine, gate_inbox=gate_inbox)

    def _event_router(self):
        """Alias of :meth:`_inbound_router` for the normalized-event path; the
        caller routes a ``ChannelEvent`` through ``route_event``."""
        return self._inbound_router()

    async def _resolve_thread_chat(self, thread_external_id: str):
        """Look up the chat bound to (this channel, thread_external_id)."""
        from primer.channel.chat_router import ChatChannelRouter
        from primer.channel.correlation import CorrelationStore
        router = ChatChannelRouter(
            storage_provider=self._sp,
            correlation_store=CorrelationStore(self._sp))
        return await router._find_thread_chat(
            channel_id=self._channel.id, thread_external_id=thread_external_id)

    # -- shared outbound-media fan-out -------------------------------------

    async def _send_media_parts(self, target: Any, parts: list) -> int:
        """Upload every hydrated media part (with ``.data`` bytes) to *target*.

        Skips parts that carry no bytes. ``target`` and the per-part upload are
        provider-specific, so the per-part send is delegated to
        :meth:`_send_media_part`. Returns the number of parts sent.
        """
        sent = 0
        for part in parts:
            data = getattr(part, "data", None)
            if not data:
                continue
            await self._send_media_part(target, part)
            sent += 1
        return sent

    async def _send_media_part(self, target: Any, part: Any) -> None:
        """Upload one media part to *target*. Provider-specific."""
        raise NotImplementedError


__all__ = [
    "attribution_header",
    "BoundedDict",
    "ChannelAdapter",
    "DEFAULT_CACHE_MAXSIZE",
    "PromptEnvelope",
    "ResponseEnvelope",
    "provider_supports_threads",
]
