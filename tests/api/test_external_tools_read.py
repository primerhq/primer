"""Read surface for external tool calls (pending + global list)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from primer.model.external_tool import ExternalToolCall


async def _seed_call(fake_storage_provider, **over):
    row_kwargs = dict(
        session_id="sess-r1",
        tool_call_id="tc-1",
        tool_name="lookup_customer",
        arguments={"id": "c1"},
        created_at=datetime.now(UTC),
    )
    row_kwargs.update(over)
    row = ExternalToolCall(**row_kwargs)
    await fake_storage_provider.get_storage(ExternalToolCall).create(row)
    return row


async def test_session_pending_lists_only_pending(
    client, fake_storage_provider
):
    await _seed_call(fake_storage_provider)
    await _seed_call(
        fake_storage_provider, tool_call_id="tc-2", status="completed"
    )
    r = await client.get("/v1/sessions/sess-r1/external_tools/pending")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["tool_call_id"] for i in items] == ["tc-1"]
    assert items[0]["tool_name"] == "lookup_customer"


async def test_chat_pending_scopes_by_chat(client, fake_storage_provider):
    await _seed_call(
        fake_storage_provider,
        session_id=None,
        chat_id="chat-r1",
        tool_call_id="tc-3",
    )
    r = await client.get("/v1/chats/chat-r1/external_tools/pending")
    assert r.status_code == 200, r.text
    assert [i["tool_call_id"] for i in r.json()["items"]] == ["tc-3"]


async def test_global_list_filters_and_pages(client, fake_storage_provider):
    await _seed_call(fake_storage_provider, tool_call_id="tc-4")
    await _seed_call(
        fake_storage_provider, tool_call_id="tc-5", status="cancelled"
    )
    r = await client.get(
        "/v1/external_tool_calls", params={"status": "pending"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] >= 1
    assert all(i["status"] == "pending" for i in body["items"])
    assert any(i["tool_call_id"] == "tc-4" for i in body["items"])


async def test_expired_pending_reports_timed_out(
    client, fake_storage_provider
):
    await _seed_call(
        fake_storage_provider,
        tool_call_id="tc-6",
        timeout_at=datetime.now(UTC) - timedelta(seconds=5),
    )
    r = await client.get("/v1/sessions/sess-r1/external_tools/pending")
    assert "tc-6" not in [i["tool_call_id"] for i in r.json()["items"]]
    g = await client.get(
        "/v1/external_tool_calls", params={"session_id": "sess-r1"}
    )
    by_id = {i["tool_call_id"]: i for i in g.json()["items"]}
    assert by_id["tc-6"]["status"] == "timed_out"
    assert by_id["tc-6"]["result"] == {"timed_out": True}
