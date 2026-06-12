"""Slack native token streaming with a single-postMessage fallback.

chat.startStream / chat.appendStream / chat.stopStream shipped Oct 2025.
On any stream error / 429 / missing method we post the full text once.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def stream_or_post(
    *, client, channel: str, thread_ts: str | None, text: str,
) -> None:
    """Stream ``text`` into the thread, or post it whole on any failure."""
    start = getattr(client, "chat_startStream", None)
    append = getattr(client, "chat_appendStream", None)
    stop = getattr(client, "chat_stopStream", None)
    if start is None or append is None or stop is None:
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=text)
        return
    try:
        started = await start(channel=channel, thread_ts=thread_ts)
        stream_ts = started.get("ts") if isinstance(started, dict) else None
        await append(channel=channel, ts=stream_ts, markdown_text=text)
        await stop(channel=channel, ts=stream_ts)
    except Exception as exc:
        logger.warning("slack stream failed (%s); falling back to post", exc)
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=text)


__all__ = ["stream_or_post"]
