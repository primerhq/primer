"""A complete Primer tool loop with synthetic identity and model responses."""
import asyncio
import json
import os
import re
import secrets

import httpx
import pytest

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.llm.codex.broker.service import BrokerServer
from primer.llm.codex.request import alias
from tests.llm.codex import test_broker
from tests.llm.codex.test_bridge import frames
from tests.llm.codex.test_broker import rsa_key as rsa_key, world as world
from tests.llm.codex.test_factory import binding as binding, local_peer as local_peer


def workspace_responses(request):
    """Derive the write from the previous tool result on the actual model wire."""
    history = request["input"]
    outputs = [item for item in history if item["type"] == "function_call_output"]
    if len(outputs) == 2:
        assert outputs[-1]["call_id"] == "write_receipt"
        return frames(text="Receipt copied.", response_id="final")

    if outputs:
        assert len(outputs) == 1 and outputs[0]["call_id"] == "read_receipt"
        receipt = re.search(r"\b[0-9a-f]{32}\b", outputs[0]["output"])
        assert receipt, "the read result was missing from the next inference request"
        tool, call_id = "workspace__write", "write_receipt"
        args = {"path": "output.txt", "content": receipt.group()}
    else:
        tool, call_id = "workspace__read", "read_receipt"
        args = {"path": "input.txt"}

    assert alias(tool) in {decl["name"] for decl in request["tools"][0]["tools"]}
    item = {"type": "function_call", "namespace": "primer", "name": alias(tool),
            "id": f"item_{call_id}", "call_id": call_id, "arguments": json.dumps(args)}
    events = frames(response_id=call_id, text="")
    events[-1]["response"]["output"] = [item]
    events[-1:-1] = [
        {"type": "response.output_item.added", "output_index": 1, "item": {**item, "arguments": ""}},
        {"type": "response.output_item.done", "output_index": 1, "item": item},
    ]
    return events


@pytest.mark.asyncio
async def test_workspace_session_replays_tool_results_and_writes_receipt(
    fake_storage_provider, world, binding, local_peer, monkeypatch, tmp_path,
):
    await world.login()
    monkeypatch.setattr(test_broker, "frames", lambda: workspace_responses(json.loads(world.calls[-1].content)))
    server = BrokerServer(world.broker, peer_uid=os.geteuid(), account=world.account)
    listener = await asyncio.start_unix_server(server.handle, binding.trust_directory / "model.sock")
    registry = ProviderRegistry(fake_storage_provider, codex_brokers={"cx": binding})
    app = create_test_app(storage_provider=fake_storage_provider, provider_registry=registry, start_chat_worker=True)
    root = tmp_path / "workspaces"
    root.mkdir()

    async with listener, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async def create(path, body):
            response = await client.post(f"/v1/{path}", json=body)
            response.raise_for_status()
            return response.json()

        await create("auth/register", {"username": "operator", "password": secrets.token_urlsafe(18)})
        await create("llm_providers", {"id": "cx", "provider": "codex", "config": {}, "limits": {"max_concurrency": 1}})
        await create("model_profiles", {"id": "profile", "description": "broker fixture", "provider_id": "cx",
                                        "model_name": "model", "context_length": 32768})
        await create("agents", {"id": "agent", "description": "receipt test", "model": {"profile_id": "profile"},
                                "tools": [], "system_prompt": ["Use the workspace tools."]})
        await create("workspace_providers", {"id": "local-store", "provider": "local",
                                              "config": {"kind": "local", "root_path": str(root)}})
        await create("workspace_templates", {"id": "template", "description": "receipt workspace",
                                             "provider_id": "local-store", "backend": {"kind": "local"}})
        workspace = await create("workspaces", {"template_id": "template"})
        directory = root / workspace["id"]
        receipt = secrets.token_hex(16)
        (directory / "input.txt").write_text(receipt)
        await app.state.start_worker_pool()
        try:
            session = await create(f"workspaces/{workspace['id']}/sessions", {
                "binding": {"kind": "agent", "agent_id": "agent"}, "auto_start": True,
                "initial_instructions": "Copy input.txt into output.txt and confirm completion.",
            })
            async with asyncio.timeout(30):
                while True:
                    response = await client.get(f"/v1/sessions/{session['id']}/messages", params={"limit": 500})
                    response.raise_for_status()
                    items = response.json()["items"]
                    errors = [item["payload"] for item in items if item["kind"] == "error"]
                    assert not errors, errors
                    if any(item["kind"] == "assistant_token" and "Receipt copied." in item["payload"]["text"]
                           for item in items):
                        break
                    await asyncio.sleep(0.1)

            calls = [item["payload"] for item in items if item["kind"] == "tool_call"]
            results = [item["payload"] for item in items if item["kind"] == "tool_result"]
            assert [call["name"] for call in calls] == ["workspace__read", "workspace__write"]
            assert [call["id"] for call in calls] == [result["call_id"] for result in results]
            assert not any(result["error"] for result in results)
            assert (directory / "output.txt").read_text() == receipt
        finally:
            await app.state.stop_worker_pool()
            await registry.aclose()
