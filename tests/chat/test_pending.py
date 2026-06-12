import pytest

from primer.chat.pending import abandon_pending_rows
from primer.model.chats import Chat


class _Chats:
    def __init__(self, stored): self.stored = stored; self.updated = None
    async def get(self, cid): return self.stored
    async def update(self, chat): self.updated = chat.model_copy(deep=True)


class _Msgs:
    def __init__(self): self.rows = []
    async def create(self, row): self.rows.append(row)


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_abandon_pending_rows_appends_rejection_and_clears():
    chat = Chat(id="c1", agent_id="a", created_at=_now(), last_seq=3,
                pending_tool_call={"tool_call_id": "tc1", "mode": "approval"})
    stored = chat.model_copy(deep=True)
    chats = _Chats(stored)
    msgs = _Msgs()
    await abandon_pending_rows(
        chat, pending=chat.pending_tool_call, messages=msgs, chats=chats,
        result_text="auto-rejected: agent switched", terminal_reason="agent_switch",
    )
    kinds = [r.kind for r in msgs.rows]
    assert kinds == ["tool_result", "cancelled"]
    tr = msgs.rows[0].payload
    assert tr["id"] == "tc1" and tr["error"] is True
    assert tr["result"] == "auto-rejected: agent switched"
    assert msgs.rows[1].payload["reason"] == "agent_switch"
    assert chats.updated.pending_tool_call is None
    assert chats.updated.last_seq == 5
