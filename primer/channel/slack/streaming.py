"""Slack native token streaming with a single-postMessage fallback.

chat.startStream / chat.appendStream / chat.stopStream shipped Oct 2025.
On any stream error / 429 / missing method we post the full text once.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def stream_or_post(
    *, client, channel: str, thread_ts: str | None, text: str,
    team_id: str | None = None, recipient_user_id: str | None = None,
) -> None:
    """Stream ``text`` into the thread, or post it whole when streaming does not
    apply.

    ``chat.startStream`` is an assistant API: it streams a reply addressed to a
    specific user and requires BOTH ``recipient_team_id`` and
    ``recipient_user_id``. A channel relay has no single recipient, so without
    both ids (the common case) we post the message whole - which is also the
    failure fallback. This avoids a guaranteed ``missing_recipient_user_id``
    round-trip on every channel reply.
    """
    start = getattr(client, "chat_startStream", None)
    append = getattr(client, "chat_appendStream", None)
    stop = getattr(client, "chat_stopStream", None)
    if not (start and append and stop and team_id and recipient_user_id):
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=text)
        return
    try:
        started = await start(
            channel=channel, thread_ts=thread_ts,
            recipient_team_id=team_id, recipient_user_id=recipient_user_id)
        stream_ts = started.get("ts") if isinstance(started, dict) else None
        await append(channel=channel, ts=stream_ts, markdown_text=text)
        await stop(channel=channel, ts=stream_ts)
    except Exception as exc:
        logger.warning("slack stream failed (%s); falling back to post", exc)
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=text)


__all__ = ["stream_or_post"]
