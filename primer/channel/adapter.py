"""Channel adapter ABC + provider-agnostic envelope types."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any

from primer.model.channel import ChannelProviderType
# Envelopes live in core model so the agent/worker layers never import
# primer.channel; re-exported here because every channel adapter and
# a good deal of test code imports them from this module.
from primer.model.envelope import PromptEnvelope, ResponseEnvelope

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


#: What a chat user is told when their approval click or rejection reason was refused because the gate is routed to specific approvers
#: (``ChannelInbox`` raises ``ApproverRefusedError``: a messaging-platform user is not an identified primer user). Under 200 characters:
#: Telegram's alert on a callback query takes no more.
APPROVAL_ROUTED_NOTICE = (
    "This approval is routed to specific approvers, so it cannot be decided from a chat. "
    "Decide it in the console, as one of its approvers or an admin."
)

#: What a chat user is told when their click named an approval that has since been replaced by a newer one under the same tool call id
#: (``ChannelInbox`` raises ``StaleGateError``, C-033). Nothing was decided. Under 200 characters, like the notice above.
APPROVAL_STALE_NOTICE = "This approval was replaced by a newer one, so nothing was decided. Use the newest approval request in this chat."

#: The same for a reply to an ask_user question that has since been replaced under the same tool call id.
QUESTION_STALE_NOTICE = "This question was replaced by a newer one, so your reply was not sent. Reply to the newest question in this chat."


class DecisionRefused:
    """What :meth:`ChannelAdapter._handle_decision` returns for a decision that was NOT accepted: falsy, and ``notice`` is what to tell the clicker.

    A caller that only asks ``if not accepted`` keeps working; one that tells the clicker why reads ``refusal_notice(accepted)``.
    """

    __slots__ = ("notice",)

    def __init__(self, notice: str) -> None:
        self.notice = notice

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"DecisionRefused({self.notice!r})"


def refusal_notice(accepted: Any) -> str:
    """The words for a decision that was not accepted: its own notice, else the routed-to-approvers one."""
    return getattr(accepted, "notice", None) or APPROVAL_ROUTED_NOTICE


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
    # S6: the inbound path creates and steers sessions, so it needs the
    # session collaborators too. Declared with None defaults on the ABC so an
    # adapter that predates the wiring still builds.
    _workspace_registry: Any = None
    _scheduler: Any = None

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
        gate_id: str | None = None,
    ) -> "bool | DecisionRefused":
        """Relay a tool-approval decision (Approve/Reject) to the inbox.

        The acting user's id is recorded under the provider-specific
        :meth:`_user_id_key` so renderers can attribute the decision.

        ``gate_id`` is the id (or its first 12 characters) of the gate the click was drawn from, kept with the button by the platform (C-033).

        Returns ``True`` when the inbox accepted the decision and a falsy :class:`DecisionRefused` when it
        REFUSED it: because the gate is routed to specific approvers
        (:class:`primer.session.approvers.ApproverRefusedError`; a messaging-platform
        user is not a primer user the spec can admit, so such a gate is decided in
        the console, see ``ChannelInbox._enforce_approvers``) or because the click is for a
        gate that has since been replaced (:class:`primer.session.gate_token.StaleGateError`).
        The caller then tells the clicker ``refusal_notice(accepted)`` and must not mark the
        message decided. Any other failure still raises.
        """
        from primer.session.approvers import ApproverRefusedError
        from primer.session.gate_token import StaleGateError

        try:
            await self._inbox.handle_response(ResponseEnvelope(
                kind="tool_approval",
                workspace_id=workspace_id, session_id=session_id,
                tool_call_id=tool_call_id,
                response=None, decision=decision, reason=reason,
                platform_metadata={
                    self._user_id_key(): user_id
                    if user_id is not None else self._user_id_default(),
                },
                gate_id=gate_id,
            ))
        except ApproverRefusedError:
            return DecisionRefused(APPROVAL_ROUTED_NOTICE)   # the inbox already logged the refusal with the platform metadata
        except StaleGateError:
            return DecisionRefused(APPROVAL_STALE_NOTICE)    # and this one, too
        return True

    async def _handle_text_reply(
        self, *,
        workspace_id: str, session_id: str, tool_call_id: str,
        text: str,
        user_id: Any = None,
        gate_id: str | None = None,
    ) -> "bool | DecisionRefused":
        """Relay a free-text ask_user reply to the inbox.

        ``gate_id`` is the id of the prompt the reply was correlated to (the persistent row of the message it replies to, C-033). A reply to a
        prompt that has since been replaced is refused by the inbox (:class:`primer.session.gate_token.StaleGateError`) and answers nothing: this
        returns a falsy :class:`DecisionRefused` whose notice says so, for the caller to tell the person who replied. ``True`` otherwise.
        """
        from primer.session.gate_token import StaleGateError

        try:
            await self._inbox.handle_response(ResponseEnvelope(
                kind="ask_user",
                workspace_id=workspace_id, session_id=session_id,
                tool_call_id=tool_call_id,
                response=text, decision=None, reason=None,
                platform_metadata={
                    self._user_id_key(): user_id
                    if user_id is not None else self._user_id_default(),
                },
                gate_id=gate_id,
            ))
        except StaleGateError:
            return DecisionRefused(QUESTION_STALE_NOTICE)    # the inbox already logged the refusal
        return True

    # -- shared inbound routing --------------------------------------------

    async def collect_inbound_media(self, raw: Any) -> list:
        """Build artifact-backed media parts for one raw inbound message.

        Default: no media. Platform adapters override by delegating to their
        own download helper; the factory passes the result to
        ``route_event(media_parts=...)`` (S6 section 6).
        """
        del raw
        return []

    def _inbound_router(self):
        """Build a :class:`ChannelInboundRouter` from the adapter's wiring, or
        ``None`` when inbound dispatch is not configured (no storage
        provider).
        """
        if self._sp is None:
            return None
        from primer.channel.correlation import CorrelationStore
        from primer.channel.inbound_router import ChannelInboundRouter
        return ChannelInboundRouter(
            self._sp, CorrelationStore(self._sp), event_bus=self._bus,
            claim_engine=self._claim_engine,
            scheduler=getattr(self, "_scheduler", None),
            workspace_registry=getattr(self, "_workspace_registry", None),
            artifact_registry=getattr(self, "_artifacts", None),
        )

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
