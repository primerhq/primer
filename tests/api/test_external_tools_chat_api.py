"""Chat send-path external-tools dispatch rule (REST surface)."""

from __future__ import annotations

from datetime import UTC, datetime

from primer.model.agent import Agent, AgentModel
from primer.model.chats import Chat, ChatMessage
from primer.model.external_tool import ExternalToolCall

DEF = {
    "name": "lookup_customer",
    "description": "Look up a customer.",
    "schema": {"type": "object"},
}


async def _seed_chat(sp, *, allow: bool, pending: dict | None = None) -> str:
    await sp.get_storage(Agent).create(
        Agent(
            id="agent-chat-ext",
            description="d",
            model=AgentModel(profile_id="prof-1"),
            allow_external_tools=allow,
        )
    )
    chat = Chat(
        id="chat-ext-1",
        agent_id="agent-chat-ext",
        created_at=datetime.now(UTC),
    )
    if pending is not None:
        chat.pending_tool_call = pending
    await sp.get_storage(Chat).create(chat)
    return chat.id


def _pending() -> dict:
    return {
        "tool_call_id": "tc-1",
        "mode": "external",
        "name": "lookup_customer",
        "arguments": {},
        "external_call_row_id": "etool-c1",
    }


async def _seed_call(sp) -> None:
    await sp.get_storage(ExternalToolCall).create(
        ExternalToolCall(
            id="etool-c1",
            chat_id="chat-ext-1",
            tool_call_id="tc-1",
            tool_name="lookup_customer",
            arguments={},
            created_at=datetime.now(UTC),
        )
    )


async def test_send_external_tools_flag_gate(client, fake_storage_provider):
    chat_id = await _seed_chat(fake_storage_provider, allow=False)
    r = await client.post(
        f"/v1/chats/{chat_id}/messages",
        json={"content": "hi", "external_tools": [DEF]},
    )
    assert r.status_code == 422, r.text
    assert "allow_external_tools" in r.text


async def test_send_stamps_defs_on_chat(client, fake_storage_provider):
    chat_id = await _seed_chat(fake_storage_provider, allow=True)
    r = await client.post(
        f"/v1/chats/{chat_id}/messages",
        json={"content": "hi", "external_tools": [DEF]},
    )
    assert r.status_code == 202, r.text
    chat = await fake_storage_provider.get_storage(Chat).get(chat_id)
    assert chat.external_tools
    assert chat.external_tools[0]["name"] == "lookup_customer"


async def test_pure_tool_results_stamps_pending_and_resolves_row(
    client, fake_storage_provider
):
    chat_id = await _seed_chat(
        fake_storage_provider, allow=True, pending=_pending()
    )
    await _seed_call(fake_storage_provider)
    r = await client.post(
        f"/v1/chats/{chat_id}/messages",
        json={"tool_results": [{"tool_call_id": "tc-1", "result": "ok"}]},
    )
    assert r.status_code == 202, r.text
    chat = await fake_storage_provider.get_storage(Chat).get(chat_id)
    assert chat.pending_tool_call["external_result"] == {
        "result": "ok",
        "is_error": False,
    }
    assert chat.turn_status == "claimable"
    row = await fake_storage_provider.get_storage(ExternalToolCall).get(
        "etool-c1"
    )
    assert row.status == "completed"
    # No user_message row was appended for a pure-results body.
    assert chat.last_seq == 0


async def test_mismatched_tool_result_409s(client, fake_storage_provider):
    chat_id = await _seed_chat(
        fake_storage_provider, allow=True, pending=_pending()
    )
    await _seed_call(fake_storage_provider)
    r = await client.post(
        f"/v1/chats/{chat_id}/messages",
        json={"tool_results": [{"tool_call_id": "tc-nope", "result": "?"}]},
    )
    assert r.status_code == 409, r.text
    row = await fake_storage_provider.get_storage(ExternalToolCall).get(
        "etool-c1"
    )
    assert row.status == "pending"


async def test_plain_message_cancels_external_pending(
    client, fake_storage_provider
):
    chat_id = await _seed_chat(
        fake_storage_provider, allow=True, pending=_pending()
    )
    await _seed_call(fake_storage_provider)
    r = await client.post(
        f"/v1/chats/{chat_id}/messages", json={"content": "nevermind"}
    )
    assert r.status_code == 202, r.text
    chat = await fake_storage_provider.get_storage(Chat).get(chat_id)
    assert chat.pending_tool_call is None
    row = await fake_storage_provider.get_storage(ExternalToolCall).get(
        "etool-c1"
    )
    assert row.status == "cancelled"
    assert row.result["reason"] == "superseded by new user message"
    # The abandoned pending got its pairing rows (tool_result + terminal).
    msgs = fake_storage_provider.get_storage(ChatMessage)
    from primer.model.storage import OffsetPage
    from primer.storage.q import Q

    page = await msgs.find(
        Q(ChatMessage).where("chat_id", chat_id).build(),
        OffsetPage(offset=0, length=50),
    )
    kinds = [m.kind for m in page.items]
    assert "tool_result" in kinds and "cancelled" in kinds


async def test_empty_body_rejected(client, fake_storage_provider):
    chat_id = await _seed_chat(fake_storage_provider, allow=True)
    r = await client.post(f"/v1/chats/{chat_id}/messages", json={})
    assert r.status_code == 422
