"""One-way inform delivery sink for the inform_user tool.

A sink is an async callable ``(message: str) -> int`` returning the number of
destinations actually reached. SessionInformSink fans the message to the
session's channels (best-effort) via a plain ``inform`` PromptEnvelope.

Chat-surface inform delivery is intentionally not implemented here yet: appending
the line mid-tool-dispatch would split a multi-tool batch's tool_result rows and
corrupt the next turn's reconstructed history. It is deferred to the
channels-drive-chats sub-project, which will persist it without that hazard.
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
        # The dispatcher returns one dict per channel; a failed delivery is an
        # ``{"error": ...}`` entry. Count only the channels actually reached.
        return sum(
            1 for r in (results or [])
            if not (isinstance(r, dict) and "error" in r)
        )
