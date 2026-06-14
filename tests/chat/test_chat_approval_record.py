"""A chat-surface approval decision writes a durable ToolApprovalRecord.

Covers the two finalization paths on the chat surface:
* ``resume_pending`` -> operator approved / rejected.
* ``abandon_pending`` -> cancel-while-awaiting on an approval gate.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.chat.executor import ChatTurnRunner
from primer.model.chats import Chat, ChatMessage
from primer.model.chat import ToolResultPart
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord

from tests.conftest import _FakeStorageProvider


class _FakeTools:
    async def execute(self, call, *, bypass_approval=False):
        return ToolResultPart(id=call.id, output="ran", error=False)


def _pending(tcid="ctc-1"):
    return {
        "tool_call_id": tcid,
        "mode": "approval",
        "original_call": {"id": tcid, "name": "send_money", "arguments": {"amt": 99}},
        "policy_id": "pp",
        "approval_type": "policy",
        "gate_reason": "spend over limit",
    }


async def _runner_and_storage():
    sp = _FakeStorageProvider()
    chats = sp.get_storage(Chat)
    msgs = sp.get_storage(ChatMessage)
    records = sp.get_storage(ToolApprovalRecord)
    runner = ChatTurnRunner(
        agent=object(),
        llm=object(),
        llm_model=object(),
        tool_manager=_FakeTools(),
        chat_storage=chats,
        message_storage=msgs,
        approval_record_storage=records,
    )
    chat = Chat(id="chat-1", agent_id="agt", created_at=datetime.now(UTC),
                status="active", pending_tool_call=_pending())
    await chats.create(chat)
    return runner, chat, records, msgs


async def _all_records(records):
    page = await records.list(OffsetPage(offset=0, length=50))
    return page.items


async def _make_reply(msgs, text: str) -> ChatMessage:
    """Persist the human reply row so resume_pending can flag it excluded."""
    reply = ChatMessage(
        id=ChatMessage.make_id("chat-1", 5), chat_id="chat-1", seq=5,
        kind="user_message", payload={"content": text}, created_at=datetime.now(UTC),
    )
    await msgs.create(reply)
    return reply


@pytest.mark.asyncio
async def test_chat_resume_approved_writes_record():
    runner, chat, records, msgs = await _runner_and_storage()
    chat.last_seq = 1
    reply = await _make_reply(msgs, "yes")
    await runner.resume_pending(chat, _pending(), reply)
    items = await _all_records(records)
    assert len(items) == 1
    rec = items[0]
    assert rec.decision == "approved"
    assert rec.chat_id == "chat-1"
    assert rec.session_id is None
    assert rec.tool_name == "send_money"
    assert rec.arguments == {"amt": 99}
    assert rec.policy_id == "pp"
    assert rec.approval_type == "policy"
    assert rec.gate_reason == "spend over limit"


@pytest.mark.asyncio
async def test_chat_resume_rejected_writes_record():
    runner, chat, records, msgs = await _runner_and_storage()
    chat.last_seq = 1
    reply = await _make_reply(msgs, "no way")
    await runner.resume_pending(chat, _pending(), reply)
    items = await _all_records(records)
    assert len(items) == 1
    assert items[0].decision == "rejected"
    assert items[0].reason == "no way"


@pytest.mark.asyncio
async def test_chat_cancel_while_awaiting_writes_record():
    runner, chat, records, _msgs = await _runner_and_storage()
    await runner.abandon_pending(chat, _pending())
    items = await _all_records(records)
    assert len(items) == 1
    assert items[0].decision == "cancelled"
    assert items[0].reason == "cancelled by user"


@pytest.mark.asyncio
async def test_chat_resume_writes_record_exactly_once():
    runner, chat, records, msgs = await _runner_and_storage()
    chat.last_seq = 1
    reply = await _make_reply(msgs, "approve")
    await runner.resume_pending(chat, _pending(), reply)
    items = await _all_records(records)
    assert len(items) == 1
