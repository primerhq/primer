"""One-way inform delivery sinks for the inform_user tool.

A sink is an async callable ``(message: str) -> int`` returning the number of
destinations reached. SessionInformSink fans the message to the session's
channels (best-effort) via a plain ``inform`` PromptEnvelope; ChatInformSink
appends the message as an assistant-visible line so a chat user sees it inline.
"""

from __future__ import annotations

import logging
from typing import Any

from primer.channel.adapter import PromptEnvelope


logger = logging.getLogger(__name__)


class SessionInformSink:
    def __init__(self, *, dispatcher: Any | None,
                 workspace_id: str, session_id: str) -> None:
        self._dispatcher = dispatcher
        self._workspace_id = workspace_id
        self._session_id = session_id

    async def __call__(self, message: str) -> int:
        if self._dispatcher is None:
            return 0
        envelope = PromptEnvelope(
            kind="inform",
            workspace_id=self._workspace_id,
            session_id=self._session_id,
            tool_call_id="",
            prompt=message,
            response_schema=None,
            choices=None,
            timeout_at_iso=None,
        )
        try:
            results = await self._dispatcher.dispatch_prompt(envelope=envelope)
        except Exception:
            logger.exception("SessionInformSink: dispatch failed")
            return 0
        return len(results or [])


class ChatInformSink:
    def __init__(self, *, runner: Any, chat: Any) -> None:
        self._runner = runner
        self._chat = chat

    async def __call__(self, message: str) -> int:
        await self._runner._append(
            self._chat, kind="assistant_token", payload={"delta": message},
        )
        return 1
