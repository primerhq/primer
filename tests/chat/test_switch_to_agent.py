import pytest
from primer.model.yield_ import Yielded, YieldToWorker
from primer.chat.executor import ChatTurnRunner
from primer.model.chats import Chat


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


class _Chats:
    def __init__(self, stored): self.stored = stored; self.updated = None
    async def get(self, cid): return self.stored
    async def update(self, chat): self.updated = chat.model_copy(deep=True); self.stored = self.updated


@pytest.mark.asyncio
async def test_handle_switch_sets_agent_and_queues_handoff():
    chat = Chat(id="c1", agent_id="agent-A", created_at=_now(), last_seq=2)
    chats = _Chats(chat.model_copy(deep=True))
    runner = ChatTurnRunner.__new__(ChatTurnRunner)
    runner._chats = chats
    exc = YieldToWorker(
        Yielded(tool_name="switch_to_agent", event_key="",
                resume_metadata={"agent_id": "agent-B", "prompt": "take over: do X"}),
        tool_call_id="tc1")
    await runner.handle_switch(chat, exc)
    assert chat.agent_id == "agent-B"
    assert chat.pending_handoff == "take over: do X"
    assert chat.pending_tool_call is None
