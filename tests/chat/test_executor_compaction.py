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


def _row(seq: int, kind: str, payload: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        id=ChatMessage.make_id("c1", seq),
        chat_id="c1",
        seq=seq,
        kind=kind,  # type: ignore[arg-type]
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )


def _stub_message_storage(rows: list[ChatMessage]) -> AsyncMock:
    """Build a message-storage mock whose ``find`` returns ``rows`` in
    one page.

    ``_read_messages_full`` calls ``find(predicate, CursorPage(...))``
    and loops until ``next_cursor`` is falsy. A single-page response
    with ``next_cursor=None`` is enough for these tests.
    """
    page = MagicMock()
    page.items = rows
    page.next_cursor = None
    storage = AsyncMock()
    storage.find = AsyncMock(return_value=page)
    return storage


class TestHistoryReassembly:
    async def test_load_history_replaces_pre_marker_rows_with_summary(
        self,
    ) -> None:
        """A ``compaction_marker`` row collapses every prior row into
        a single synthetic assistant message carrying ``payload.summary``;
        rows after the marker are translated as usual."""
        rows = [
            _row(1, "user_message", {"content": "old 1"}),
            _row(2, "assistant_token", {"delta": "old reply"}),
            _row(3, "done", {"stop_reason": "stop"}),
            _row(
                4, "compaction_marker",
                {
                    "summary": "SUMMARY HERE",
                    "replaced_from_seq": 1,
                    "replaced_to_seq": 3,
                    "model": "gpt-4o",
                    "tokens_before": 999,
                    "tokens_after": 9,
                    "compaction_prompt_source": "default",
                    "created_at": "2026-05-30T14:30:00Z",
                },
            ),
            _row(5, "user_message", {"content": "new"}),
            _row(6, "assistant_token", {"delta": "fresh reply"}),
            _row(7, "done", {"stop_reason": "stop"}),
        ]
        storage = _stub_message_storage(rows)
        runner = _make_runner(message_storage=storage)

        loaded = await runner._load_history("c1")

        # synthetic assistant summary + user "new" + assistant "fresh reply"
        assert len(loaded) == 3
        assert loaded[0].role == "assistant"
        assert loaded[0].parts[0].text == "SUMMARY HERE"
        assert loaded[1].role == "user"
        assert loaded[1].parts[0].text == "new"
        assert loaded[2].role == "assistant"
        assert loaded[2].parts[0].text == "fresh reply"

    async def test_load_history_uses_last_marker_when_multiple(self) -> None:
        """If the chat has more than one marker (rare — only possible
        after repeated forced compactions), only the last one's summary
        survives. Older rows are dropped entirely."""
        rows = [
            _row(1, "user_message", {"content": "v1"}),
            _row(
                2, "compaction_marker",
                {"summary": "FIRST PASS", "replaced_from_seq": 1, "replaced_to_seq": 1},
            ),
            _row(3, "user_message", {"content": "v2"}),
            _row(
                4, "compaction_marker",
                {"summary": "SECOND PASS", "replaced_from_seq": 1, "replaced_to_seq": 3},
            ),
            _row(5, "user_message", {"content": "v3"}),
            _row(6, "assistant_token", {"delta": "after"}),
            _row(7, "done", {"stop_reason": "stop"}),
        ]
        storage = _stub_message_storage(rows)
        runner = _make_runner(message_storage=storage)

        loaded = await runner._load_history("c1")

        # synthetic summary + user "v3" + assistant "after"
        assert len(loaded) == 3
        assert loaded[0].role == "assistant"
        assert loaded[0].parts[0].text == "SECOND PASS"
        assert loaded[1].role == "user"
        assert loaded[1].parts[0].text == "v3"
        assert loaded[2].role == "assistant"
        assert loaded[2].parts[0].text == "after"

    async def test_load_history_unaffected_when_no_marker(self) -> None:
        """No marker row → translation matches the pre-compaction path."""
        rows = [
            _row(1, "user_message", {"content": "hi"}),
            _row(2, "assistant_token", {"delta": "hello"}),
            _row(3, "done", {"stop_reason": "stop"}),
        ]
        storage = _stub_message_storage(rows)
        runner = _make_runner(message_storage=storage)

        loaded = await runner._load_history("c1")

        assert len(loaded) == 2
        assert loaded[0].role == "user"
        assert loaded[0].parts[0].text == "hi"
        assert loaded[1].role == "assistant"
        assert loaded[1].parts[0].text == "hello"


class TestUsageTracking:
    async def test_records_input_and_output_tokens(self) -> None:
        """``_record_usage`` updates the runner's ``_last_input_tokens``
        and ``_last_output_tokens`` so callers (or future plumbing
        hooks) can read the most recent count."""
        runner = _make_runner()
        runner._record_usage(
            Usage(input_tokens=1234, output_tokens=56, cumulative=False),
        )
        assert runner._last_input_tokens == 1234
        assert runner._last_output_tokens == 56

    async def test_record_usage_overwrites_on_each_call(self) -> None:
        """Each Usage event replaces the prior count (no accumulation
        — the provider's ``cumulative`` flag decides that semantic)."""
        runner = _make_runner()
        runner._record_usage(
            Usage(input_tokens=100, output_tokens=10, cumulative=False),
        )
        runner._record_usage(
            Usage(input_tokens=250, output_tokens=42, cumulative=False),
        )
        assert runner._last_input_tokens == 250
        assert runner._last_output_tokens == 42
