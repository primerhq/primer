"""ChatTurnRunner compaction integration: pre-turn auto-compact,
history reassembly across a compaction_marker, and Usage tracking.

The runner has a fairly opinionated constructor that wires real
LLM + storage objects. These tests don't need the full driver loop,
just direct invocation of the new private methods. We construct the
runner via ``__new__`` + manual attribute injection so the test
fixtures stay tiny and don't drift if ``__init__`` gains new
required arguments.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from primer.agent.compaction_mixin import CompactionResult
from primer.chat.executor import ChatTurnRunner
from primer.model.chat import Message, TextPart, Usage
from primer.model.chats import Chat, ChatMessage


def _make_runner(
    *,
    chat: Chat | None = None,
    llm: Any = None,
    message_storage: AsyncMock | None = None,
    chat_storage: AsyncMock | None = None,
    tool_manager: Any = None,
    agent: Any = None,
) -> ChatTurnRunner:
    """Build a bare ``ChatTurnRunner`` with mocked deps.

    Bypasses ``__init__`` so the fixture stays insulated from the
    real constructor's evolving required arguments — these tests
    target the new private methods, not construction.
    """
    runner = ChatTurnRunner.__new__(ChatTurnRunner)
    runner._agent = agent or MagicMock(compaction_prompt=None, system_prompt=None)
    runner._llm = llm or MagicMock()
    runner._model = MagicMock(name="gpt-4o", context_length=10_000)
    runner._model.name = "gpt-4o"  # MagicMock(name=...) sets the repr, not the attr.
    tm = tool_manager
    if tm is None:
        tm = MagicMock()
        tm.list_tools = AsyncMock(return_value=[])
    runner._tools = tm
    runner._chats = chat_storage or AsyncMock()
    runner._messages = message_storage or AsyncMock()
    runner._cancel_event = None
    runner._marker_persisted = False
    runner._last_input_tokens = None
    runner._last_output_tokens = None
    return runner


def _chat(last_seq: int = 10) -> Chat:
    return Chat(
        id="c1",
        agent_id="ag",
        created_at=datetime.now(timezone.utc),
        last_seq=last_seq,
    )


class TestChatRunnerCompaction:
    async def test_runs_compaction_when_over_threshold(self) -> None:
        """``should_compact`` returns triggered=True → apply_compaction
        runs → a ``compaction_marker`` row is persisted and the
        in-memory history is replaced with the compacted form."""
        # Mock the heavy lifters at module level so we don't have to
        # stand up a real LLM client.
        from primer.chat import executor as exec_mod

        async def fake_should_compact(**_kw):
            return True, 9_000

        summary_msg = Message(
            role="assistant", parts=[TextPart(text="SUMMARY ROLLED UP")],
        )
        new_history = [summary_msg]

        async def fake_apply_compaction(**_kw):
            return CompactionResult(
                new_history=new_history,
                summary_text="SUMMARY ROLLED UP",
                tokens_before=9_000,
                tokens_after=120,
                model="gpt-4o",
                created_at=datetime.now(timezone.utc),
            )

        chat = _chat(last_seq=10)
        chat_storage = AsyncMock()
        chat_storage.get = AsyncMock(return_value=chat)
        message_storage = AsyncMock()

        runner = _make_runner(
            chat_storage=chat_storage,
            message_storage=message_storage,
        )

        # Patch the mixin entry points.
        original_should = exec_mod._mixin_should_compact
        original_apply = exec_mod._mixin_apply_compaction
        exec_mod._mixin_should_compact = fake_should_compact
        exec_mod._mixin_apply_compaction = fake_apply_compaction
        try:
            history = [
                Message(role="user", parts=[TextPart(text=f"m{i}")])
                for i in range(8)
            ]
            triggered = await runner._maybe_compact_history(chat, history)
        finally:
            exec_mod._mixin_should_compact = original_should
            exec_mod._mixin_apply_compaction = original_apply

        assert triggered is True
        assert runner._marker_persisted is True
        # The mutated history reflects the compacted form.
        assert history == new_history

        # A compaction_marker ChatMessage was persisted with the right
        # payload shape.
        assert message_storage.create.await_count == 1
        persisted: ChatMessage = message_storage.create.await_args.args[0]
        assert persisted.kind == "compaction_marker"
        assert persisted.chat_id == "c1"
        assert persisted.seq == 11
        assert persisted.payload["summary"] == "SUMMARY ROLLED UP"
        assert persisted.payload["model"] == "gpt-4o"
        assert persisted.payload["tokens_before"] == 9_000
        assert persisted.payload["tokens_after"] == 120
        assert persisted.payload["compaction_prompt_source"] == "default"
        assert persisted.payload["replaced_to_seq"] == 10

    async def test_skips_when_under_threshold(self) -> None:
        """``should_compact`` returns False → no marker row + history
        untouched."""
        from primer.chat import executor as exec_mod

        async def fake_should_compact(**_kw):
            return False, 100

        chat = _chat(last_seq=10)
        chat_storage = AsyncMock()
        chat_storage.get = AsyncMock(return_value=chat)
        message_storage = AsyncMock()
        runner = _make_runner(
            chat_storage=chat_storage,
            message_storage=message_storage,
        )

        original_should = exec_mod._mixin_should_compact
        exec_mod._mixin_should_compact = fake_should_compact
        try:
            history = [Message(role="user", parts=[TextPart(text="hi")])]
            original = list(history)
            triggered = await runner._maybe_compact_history(chat, history)
        finally:
            exec_mod._mixin_should_compact = original_should

        assert triggered is False
        assert runner._marker_persisted is False
        assert message_storage.create.await_count == 0
        assert history == original
