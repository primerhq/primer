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
    BoundedDict, ChannelAdapter, PromptEnvelope,
    attribution_header, session_thread_label,
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
        # Bounded so a long-lived bot does not grow this map without limit; an
        # evicted session simply re-opens its thread on the next prompt.
        self._session_threads: BoundedDict = BoundedDict()

    def _user_id_key(self) -> str:
        return "slack_user_id"

    def _user_id_default(self) -> str:
        return ""

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
        media = getattr(envelope, "media", None)
        if media and self._artifacts is not None:
            from primer.channel.media import hydrate_media_dicts
            parts = await hydrate_media_dicts(self._artifacts, media)
            if parts:
                await self.post_chat_media(parts, thread_ts=root_ts)
        header = attribution_header(envelope)
        if envelope.kind == "inform":
            await client.chat_postMessage(
                channel=self._channel.external_id,
                thread_ts=root_ts,
                text=header + envelope.prompt,
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
        if header:
            body["text"] = header + body["text"]
        # Post every prompt into the session's conversation thread.
        body["thread_ts"] = root_ts
        resp = await client.chat_postMessage(**body)
        ts = resp.get("ts", "")
        if envelope.kind == "ask_user":
            # Persist the correlation so inbound reply routing is durable.
            if self._sp is not None:
                from primer.channel.correlation import CorrelationStore
                try:
                    await CorrelationStore(self._sp).upsert_session(
                        channel_id=self._channel.id,
                        anchor=root_ts,
                        workspace_id=envelope.workspace_id,
                        session_id=envelope.session_id,
                        tool_call_id=envelope.tool_call_id,
                    )
                except Exception:
                    logger.warning(
                        "slack: failed to persist ask_user correlation "
                        "for thread %s", root_ts, exc_info=True,
                    )
        return {"ts": ts, "channel": resp.get("channel", ""), "thread_ts": root_ts}

    async def post_chat_message(
        self, text: str, *, thread_ts: str | None = None,
    ) -> dict:
        """Outbound chat relay: post the reply to the channel/thread.

        Channel replies are not addressed to a single user, so native
        ``chat.startStream`` (an assistant-only API needing a recipient) does
        not apply; ``stream_or_post`` posts the message whole."""
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
        sent = await self._send_media_parts((client, thread_ts), parts)
        return {"sent": sent}

    async def _send_media_part(self, target: Any, part: Any) -> None:
        client, thread_ts = target
        data = getattr(part, "data", None)
        filename = getattr(part, "filename", None) or "file"
        kwargs = {
            "channel": self._channel.external_id,
            "file": data, "filename": filename,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        await client.files_upload_v2(**kwargs)

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
    # _handle_decision / _handle_text_reply / _inbound_router / _event_router
    # / _resolve_thread_chat are inherited from ChannelAdapter.

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
        from primer.channel.commands import parse_command
        parsed = parse_command(text)
        if parsed is not None:
            await self._handle_thread_command(
                parsed=parsed, thread_ts=thread_ts or message_ts)
            return None
        text, media_parts = await self._collect_inbound_media(text, files)
        router = self._inbound_router()
        if router is None:
            return None
        # Top-level message (no thread_ts) opens a new thread-chat keyed on
        # message_ts; an in-thread message routes to thread_ts's chat.
        await router.route(
            channel=self._channel,
            anchor=thread_ts,
            reply_to=message_ts,
            is_thread_channel=True,
            sender=sender_name,
            text=text,
            media_parts=media_parts or None,
        )
        # Resolve the resulting chat for callers/tests (side-effect already done).
        thread_external_id = thread_ts or message_ts
        return await self._resolve_thread_chat(thread_external_id)

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
                res = await ex.set_agent(
                    chat_id=chat.id, agent_id=parsed.arg,
                    channel_id=self._channel.id)
                await _post(text=res.text or "Agent switched.")
            else:
                picker = await ex.agent_picker(channel_id=self._channel.id)
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



__all__ = ["REJECT_MODAL_CALLBACK_ID", "SlackChannelAdapter"]
