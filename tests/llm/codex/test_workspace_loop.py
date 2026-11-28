"""A complete Primer tool loop with synthetic identity and model responses."""
import asyncio
import json
import os
import re
import secrets
from contextlib import AsyncExitStack

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
    """Derive the write and final token from tool results on the actual wire."""
    history = request["input"]
    outputs = [item for item in history if item["type"] == "function_call_output"]
    if len(outputs) == 2:
        assert [item["call_id"] for item in outputs] == ["read_receipt", "write_receipt"]
        receipt = re.search(r"\b[0-9a-f]{32}\b", outputs[0]["output"])
        assert receipt, "the final model round must still have the read result"
        return frames(text=f"Receipt copied: {receipt.group()}.", response_id="final")

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
    async with AsyncExitStack() as cleanup:
        await world.login()
        monkeypatch.setattr(test_broker, "frames", lambda: workspace_responses(json.loads(world.calls[-1].content)))
        server = BrokerServer(world.broker, peer_uid=os.geteuid(), account=world.account)
        handlers = []
        listener = None

        def accept(reader, writer):
            # Track even handlers not yet admitted to BrokerServer._active.
            handlers.append(asyncio.create_task(server.handle(reader, writer)))

        async def close_server():
            if listener is not None:
                listener.close()
            try:
                async with asyncio.timeout(10):
                    await server.close()
            finally:
                # A listener alone does not join its clients. Not-yet-started
                # handlers must enter their own writer finally before we leave.
                async with asyncio.timeout(10):
                    await asyncio.gather(*handlers, return_exceptions=True)
                    if listener is not None:
                        await listener.wait_closed()
            assert all(task.done() for task in handlers)
            assert not server._active

        cleanup.push_async_callback(close_server)
        listener = await asyncio.start_unix_server(accept, binding.trust_directory / "model.sock")
        registry = ProviderRegistry(fake_storage_provider, codex_brokers={"cx": binding})
        cleanup.push_async_callback(registry.aclose)
        app = create_test_app(storage_provider=fake_storage_provider, provider_registry=registry, start_chat_worker=True)
        cleanup.push_async_callback(app.state.event_bus.aclose)
        cleanup.push_async_callback(app.state.scheduler.aclose)
        cleanup.push_async_callback(app.state.workspace_registry.aclose)
        # Register before API setup and start: a partial start must drain too.
        cleanup.push_async_callback(app.state.stop_worker_pool)
        root = tmp_path / "workspaces"
        root.mkdir()
        client = await cleanup.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ))

        async def create(path, body):
            response = await client.post(f"/v1/{path}", json=body)
            response.raise_for_status()
            return response.json()

        await create("auth/register", {"username": "operator", "password": secrets.token_urlsafe(18)})
        await create("llm_providers", {"id": "cx", "provider": "codex", "config": {},
                                       "limits": {"max_concurrency": 1, "max_retries": 0}})
        discovered = await client.get("/v1/llm_providers/cx/discovered_models")
        discovered.raise_for_status()
        assert discovered.json() == {
            "models": [{"name": "model", "display_name": "Test model"}],
            "source": "account_policy_filtered_broker", "transport": "model_only_responses",
        }
        # Explicit synthetic fixture budget, not context inferred from discovery.
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
                detail = await client.get(f"/v1/sessions/{session['id']}")
                detail.raise_for_status()
                state = detail.json()
                assert state["last_turn_error"] is None, state
                assert state["status"] not in {"paused", "ended"}, state
                done = [item for item in items if item["kind"] == "done"]
                assert all(item["payload"]["stop_reason"] in {"tool_use", "stop"} for item in done), done
                if (done and done[-1]["payload"]["stop_reason"] == "stop"
                        and state["status"] == "waiting" and state["session_state"] == "parked"
                        and state["turn_status"] == "idle"
                        and state["completed_turn_no"] == 0 and state["turn_no"] == 1):
                    break
                await asyncio.sleep(0.1)

        # Text is not completion. Require durable Done and the normal interactive
        # worker's committed, released turn (it rests rather than becoming ENDED).
        assert [item["payload"]["stop_reason"] for item in done] == ["tool_use", "tool_use", "stop"]
        assert done[-1]["payload"]["raw_reason"] == "completed"
        assert state["ended_reason"] is None
        calls = [item for item in items if item["kind"] == "tool_call"]
        results = [item for item in items if item["kind"] == "tool_result"]
        assert [call["payload"]["name"] for call in calls] == ["workspace__read", "workspace__write"]
        assert [call["payload"]["raw_id"] for call in calls] == ["read_receipt", "write_receipt"]
        assert [call["payload"]["id"] for call in calls] == [result["payload"]["call_id"] for result in results]
        assert not any(result["payload"]["error"] for result in results)
        assert receipt in results[0]["payload"]["output"]
        assert calls[1]["payload"]["arguments"] == {"path": "output.txt", "content": receipt}
        final_tokens = [item for item in items if item["kind"] == "assistant_token"
                        and item["payload"]["text"] == f"Receipt copied: {receipt}."]
        assert len(final_tokens) == 1, items
        ordered = [calls[0], results[0], calls[1], results[1], final_tokens[0], done[-1]]
        seqs = [item["seq"] for item in ordered]
        assert seqs == sorted(set(seqs)), ordered
        assert all(item["node_id"] == done[-1]["node_id"] for item in ordered)
        assert (directory / "output.txt").read_text() == receipt
