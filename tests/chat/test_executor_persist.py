import pytest

from primer.chat.executor import ChatTurnRunner
from primer.model.chats import Chat, ChatMessage


class _Chats:
    """In-memory Chat storage stub. `get` returns the 'storage' copy
    (which an external switch has updated), `update` records the write."""
    def __init__(self, stored: Chat):
        self.stored = stored
        self.updated: Chat | None = None
    async def get(self, cid): return self.stored
    async def update(self, chat): self.updated = chat.model_copy(deep=True)


class _Msgs:
    def __init__(self): self.rows = []
    async def create(self, row): self.rows.append(row)
    async def update(self, row): pass


def _runner(chats, msgs) -> ChatTurnRunner:
    r = ChatTurnRunner.__new__(ChatTurnRunner)
    r._chats = chats
    r._messages = msgs
    return r


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_append_preserves_externally_switched_agent_id():
    stored = Chat(id="chat-1", agent_id="agent-B", created_at=_now(), last_seq=5)
    chats = _Chats(stored)
    runner = _runner(chats, _Msgs())
    stale = Chat(id="chat-1", agent_id="agent-A", created_at=_now(), last_seq=5)
    await runner._append(stale, kind="assistant_token", payload={"delta": "hi"})
    assert chats.updated is not None
    assert chats.updated.agent_id == "agent-B"
    assert chats.updated.last_seq == 6


@pytest.mark.asyncio
async def test_persist_chat_refreshes_last_seq_from_storage():
    """Defense in depth: if storage's last_seq has moved ahead of this
    turn's stale in-memory counter, `_persist_chat` refreshes it to the
    max so the next `_append` can never reuse an already-taken seq."""
    stored = Chat(id="chat-1", agent_id="a", created_at=_now(), last_seq=9)
    chats = _Chats(stored)
    runner = _runner(chats, _Msgs())
    stale = Chat(id="chat-1", agent_id="a", created_at=_now(), last_seq=4)
    await runner._persist_chat(stale)
    assert chats.updated is not None
    assert chats.updated.last_seq == 9


@pytest.mark.asyncio
async def test_persist_chat_keeps_higher_in_memory_last_seq():
    """The refresh takes the max — it must never REGRESS a fresher
    in-memory last_seq back to a stale storage value."""
    stored = Chat(id="chat-1", agent_id="a", created_at=_now(), last_seq=3)
    chats = _Chats(stored)
    runner = _runner(chats, _Msgs())
    current = Chat(id="chat-1", agent_id="a", created_at=_now(), last_seq=7)
    await runner._persist_chat(current)
    assert chats.updated is not None
    assert chats.updated.last_seq == 7
