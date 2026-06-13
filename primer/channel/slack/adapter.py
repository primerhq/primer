"""SlackChannelAdapter — one per Channel row.

Delegates connection lifecycle to the package-level
``SLACK_CONNECTIONS`` registry. Holds the per-channel rendering
+ inbound-routing logic. Multiple adapters for the same provider
share one Socket Mode connection.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from primer.channel.adapter import (
    ChannelAdapter, PromptEnvelope, ResponseEnvelope, session_thread_label,
)
from primer.channel.slack.connection import SLACK_CONNECTIONS
from primer.channel.slack.render import (
    REJECT_MODAL_CALLBACK_ID,
    build_ask_user_message,
    build_reject_modal,
    build_tool_approval_message,
)
from primer.model.channel import Channel, ChannelProvider
from primer.model.except_ import ProviderError


logger = logging.getLogger(__name__)


def _get_web_client(conn: Any) -> Any:
    """Return the AsyncWebClient on the connection.

    slack_bolt exposes it as ``app.client``; we wrap that here so
    tests can override the lookup with a stub.
    """
    return conn.app.client


class SlackChannelAdapter(ChannelAdapter):
    """Per-channel Slack adapter."""

    def __init__(
        self,
        *,
        provider: ChannelProvider,
        channel: Channel,
        inbox,
        storage_provider=None,
        event_bus=None,
        claim_engine=None,
        artifact_registry=None,
    ) -> None:
        self._provider = provider
        self._channel = channel
        self._inbox = inbox
        # Chat-surface wiring. Optional so existing callers (session/workspace
        # channels) keep working; the chat dispatch path stays inactive when
        # _sp is None.
        self._sp = storage_provider
        self._bus = event_bus
        self._claim_engine = claim_engine
        self._artifacts = artifact_registry
        self._conn: Any | None = None
        # session_id → root message ts (the per-session conversation thread).
        self._session_threads: dict[str, str] = {}
        # thread root ts → {ws, sid, tcid} for the ask_user awaiting a reply in
        # that thread (sessions park on one prompt at a time).
        self._pending_ask: dict[str, dict[str, str]] = {}

    async def initialize(self) -> None:
        self._conn = await SLACK_CONNECTIONS.acquire(self._provider)
        # Register this adapter on the shared connection so inbound
        # handlers can dispatch by channel_id.
        entry = SLACK_CONNECTIONS.entry(self._provider.id)
        if entry is not None:
            entry.adapters_by_channel_id[self._channel.external_id] = self
        # Handlers are registered once-per-connection in Task 6 (factory).

    async def aclose(self) -> None:
        entry = SLACK_CONNECTIONS.entry(self._provider.id)
        if entry is not None:
            entry.adapters_by_channel_id.pop(self._channel.external_id, None)
        if self._conn is not None:
            await SLACK_CONNECTIONS.release(self._provider)
            self._conn = None

    async def verify(self) -> None:
        if self._conn is None:
            raise ProviderError("SlackChannelAdapter used before initialize()")
        client = _get_web_client(self._conn)
        auth = await client.auth_test()
        if not auth.get("ok"):
            raise ProviderError(
                f"slack auth.test failed: {auth.get('error', 'unknown')}"
            )
        info = await client.conversations_info(channel=self._channel.external_id)
        if not info.get("ok"):
            raise ProviderError(
                f"channel {self._channel.external_id!r} not reachable: "
                f"{info.get('error', 'unknown')}"
            )

    async def post_prompt(self, envelope: PromptEnvelope) -> dict[str, Any]:
        if self._conn is None:
            raise ProviderError("SlackChannelAdapter used before initialize()")
        client = _get_web_client(self._conn)
        root_ts = await self._session_root_ts(client, envelope.session_id)
        if envelope.kind == "inform":
            await client.chat_postMessage(
                channel=self._channel.external_id,
                thread_ts=root_ts,
                text=envelope.prompt,
            )
            return {"ts": "", "channel": self._channel.external_id, "thread_ts": root_ts}
        if envelope.kind == "ask_user":
            body = build_ask_user_message(
                channel_id=self._channel.external_id, envelope=envelope,
            )
        elif envelope.kind == "tool_approval":
            body = build_tool_approval_message(
                channel_id=self._channel.external_id, envelope=envelope,
            )
        else:
            raise ProviderError(f"unknown envelope kind {envelope.kind!r}")
        # Post every prompt into the session's conversation thread.
        body["thread_ts"] = root_ts
        resp = await client.chat_postMessage(**body)
        ts = resp.get("ts", "")
        if envelope.kind == "ask_user":
            self._pending_ask[root_ts] = {
                "ws": envelope.workspace_id,
                "sid": envelope.session_id,
                "tcid": envelope.tool_call_id,
            }
        return {"ts": ts, "channel": resp.get("channel", ""), "thread_ts": root_ts}

    async def post_chat_message(
        self, text: str, *, thread_ts: str | None = None,
    ) -> dict:
        """Outbound chat relay: stream into the thread (native) or post whole."""
        from primer.channel.slack.streaming import stream_or_post
        if self._conn is None:
            raise ProviderError("SlackChannelAdapter used before initialize()")
        client = _get_web_client(self._conn)
        await stream_or_post(
            client=client, channel=self._channel.external_id,
            thread_ts=thread_ts, text=text)
        return {"ok": True}

    async def post_chat_media(
        self, parts: list, *, thread_ts: str | None = None,
    ) -> dict:
        """Outbound media relay: upload each hydrated media part to the channel
        (into the thread when set) via files_upload_v2."""
        if self._conn is None:
            raise ProviderError("SlackChannelAdapter used before initialize()")
        client = _get_web_client(self._conn)
        sent = 0
        for part in parts:
            data = getattr(part, "data", None)
            if not data:
                continue
            filename = getattr(part, "filename", None) or "file"
            kwargs = {
                "channel": self._channel.external_id,
                "file": data, "filename": filename,
            }
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            await client.files_upload_v2(**kwargs)
            sent += 1
        return {"sent": sent}

    async def _session_root_ts(self, client: Any, session_id: str) -> str:
        """Get-or-create the root message ts for this session's thread.

        The first prompt posts a small anchor message to the channel; its ts is
        the thread root every later prompt for the session replies under.
        """
        ts = self._session_threads.get(session_id)
        if ts:
            return ts
        resp = await client.chat_postMessage(
            channel=self._channel.external_id,
            text=f":thread: {session_thread_label(session_id)}",
        )
        ts = resp.get("ts", "")
        self._session_threads[session_id] = ts
        return ts

    # ----- inbound helpers (called from handlers in factory.py) -----------

    async def _handle_decision(
        self,
        *,
        ws: str, sid: str, tcid: str,
        decision: str, reason: str | None,
        slack_user_id: str | None,
    ) -> None:
        await self._inbox.handle_response(ResponseEnvelope(
            kind="tool_approval",
            workspace_id=ws, session_id=sid, tool_call_id=tcid,
            response=None, decision=decision, reason=reason,
            platform_metadata={"slack_user_id": slack_user_id or ""},
        ))

    async def _handle_text_reply(
        self,
        *,
        ws: str, sid: str, tcid: str,
        text: str,
        slack_user_id: str | None,
    ) -> None:
        await self._inbox.handle_response(ResponseEnvelope(
            kind="ask_user",
            workspace_id=ws, session_id=sid, tool_call_id=tcid,
            response=text,
            decision=None, reason=None,
            platform_metadata={"slack_user_id": slack_user_id or ""},
        ))

    async def handle_inbound_chat_message(
        self, *, thread_ts: str | None, message_ts: str,
        sender_name: str, text: str,
        files: list[dict] | None = None,
    ):
        """Multi-type inbound: top-level opens a new thread-chat; an in-thread
        message routes to that thread's chat. The thread id is message_ts on a
        top-level message (Slack threads anchor on the parent ts).

        A message typed as a /command in a thread is intercepted and handled
        in-thread (interactive /agent select, etc.) instead of being routed as
        a chat turn. Slack native slash commands carry no thread_ts, so this is
        the only path that can target a specific thread's chat.

        Slack ``event["files"]`` (images, documents, audio) are downloaded,
        stored as artifacts and delivered alongside the text as media parts;
        the text becomes the leading caption. Files that are too large or fail
        to download are skipped (the turn still lands as text)."""
        from primer.channel.chat_inbox import ChatResponseInbox
        from primer.channel.chat_router import ChatChannelRouter
        from primer.channel.commands import parse_command
        parsed = parse_command(text)
        if parsed is not None:
            await self._handle_thread_command(
                parsed=parsed, thread_ts=thread_ts or message_ts)
            return None
        text, media_parts = await self._collect_inbound_media(text, files)
        thread_external_id = thread_ts or message_ts
        gate_inbox = ChatResponseInbox(
            storage_provider=self._sp, event_bus=self._bus,
            claim_engine=self._claim_engine)
        router = ChatChannelRouter(
            storage_provider=self._sp, event_bus=self._bus, gate_inbox=gate_inbox,
            claim_engine=self._claim_engine)
        chat, _created = await router.deliver_message(
            channel_id=self._channel.id, thread_external_id=thread_external_id,
            supports_threads=True, sender_name=sender_name, text=text,
            media_parts=media_parts or None)
        return chat

    async def _collect_inbound_media(
        self, text: str, files: list[dict] | None,
    ) -> tuple[str, list]:
        """Download each Slack file, store it as an artifact and build the
        referencing chat Part. Returns ``(text, media_parts)`` where text may
        gain a " [attachment skipped: too large]" note for rejected files.

        Media is skipped wholesale when the artifact registry or the storage
        provider is absent. Per-file: oversized/disallowed media (MediaError)
        and non-200 downloads are skipped without raising."""
        if not files:
            return text, []
        if self._artifacts is None or self._sp is None:
            return text, []
        from primer.channel.media import MediaError, store_inbound_media
        store = await self._artifacts.get_default()
        token = self._provider.config.bot_token.get_secret_value()
        parts: list = []
        for f in files:
            url = f.get("url_private_download") or f.get("url_private")
            if not url:
                continue
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        url, headers={"Authorization": f"Bearer {token}"})
            except Exception:  # noqa: BLE001 — download is best-effort
                logger.warning("slack: media download raised for %s", url,
                               exc_info=True)
                continue
            if getattr(resp, "status_code", 0) != 200:
                logger.warning(
                    "slack: media download for %s returned %s", url,
                    getattr(resp, "status_code", "?"))
                continue
            try:
                part = await store_inbound_media(
                    store, data=resp.content,
                    mime_type=f.get("mimetype"),
                    filename=f.get("name") or f.get("title"))
            except MediaError:
                text = (text or "") + " [attachment skipped: too large]"
                continue
            parts.append(part)
        return text, parts

    async def _handle_thread_command(self, *, parsed, thread_ts: str) -> None:
        """Handle a /command typed inside a chat thread: render the result
        in-thread. /agent shows an interactive select dropdown seeded with
        THIS thread's chat so picking switches it."""
        from primer.channel.chat_router import ChatChannelRouter
        from primer.channel.commands import CommandExecutor, help_text
        from primer.channel.slack.blocks import build_agent_select_blocks
        if self._sp is None or self._conn is None:
            return
        client = _get_web_client(self._conn)
        channel = self._channel.external_id
        ex = CommandExecutor(storage_provider=self._sp)
        # Resolve THIS thread's chat so commands target it.
        router = ChatChannelRouter(storage_provider=self._sp)
        chat, _ = await router.resolve_or_create(
            channel_id=self._channel.id, thread_external_id=thread_ts,
            supports_threads=True)

        async def _post(text=None, blocks=None):
            kwargs = {"channel": channel, "thread_ts": thread_ts}
            if blocks is not None:
                kwargs["blocks"] = blocks
                kwargs["text"] = text or "Pick an agent:"
            else:
                kwargs["text"] = text or ""
            await client.chat_postMessage(**kwargs)

        verb = parsed.verb
        if verb == "agent":
            if parsed.arg:
                res = await ex.set_agent(chat_id=chat.id, agent_id=parsed.arg)
                await _post(text=res.text or "Agent switched.")
            else:
                picker = await ex.agent_picker()
                if not picker.items:
                    await _post(text="No agents available.")
                    return
                blocks = build_agent_select_blocks(result=picker, chat_id=chat.id)
                await _post(text="Pick an agent:", blocks=blocks)
            return
        if verb == "list":
            res = await ex.list_chats(channel_id=self._channel.id)
            if res.items:
                lines = [f"- {it['title']} ({it['agent_id']})" for it in res.items]
                await _post(text="Chats on this channel:\n" + "\n".join(lines))
            else:
                await _post(text="No chats yet.")
            return
        if verb == "help":
            await _post(text=help_text(supports_threads=True))
            return
        if verb == "new":
            await _post(
                text="Post a new top-level message in the channel to start a "
                "new chat.")
            return
        if verb == "switch":
            await _post(
                text="On Slack, each thread is its own chat - use a new thread "
                "instead of /switch.")
            return

    def pending_ask_for_thread(self, thread_ts: str) -> dict[str, str] | None:
        """The ask_user (ws/sid/tcid) awaiting a reply in this thread, if any."""
        return self._pending_ask.get(thread_ts)

    def clear_pending_ask(self, thread_ts: str) -> None:
        self._pending_ask.pop(thread_ts, None)


__all__ = ["REJECT_MODAL_CALLBACK_ID", "SlackChannelAdapter"]
