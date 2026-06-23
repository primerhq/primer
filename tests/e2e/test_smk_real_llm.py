"""REAL-LLM smoke subset: a curated few SMK turns driven by the live LM Studio
model (qwen/qwen3-vl-8b), gated ``@requires("llm:real")``.

The bulk of the agent/graph/chat SMK coverage uses the deterministic scripted
mock LLM (hermetic). These tests instead prove the real model drives an
agent run, a minimal graph run, and a chat turn end-to-end. Real-model output
is non-deterministic and slower, so the assertions are loosened but meaningful:
the turn reaches a terminal state and yields non-empty assistant output. We
never assert exact content.

Run:
    export LMSTUDIO_API_KEY=<your-lmstudio-key>
    PRIMER_RUN_E2E=1 PRIMER_E2E_PORT=8765 uv run pytest \
        tests/e2e/test_smk_real_llm.py -p no:cacheprovider -n0 -q
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tests._support.runs import (
    make_graph,
    make_local_workspace,
    make_real_agent,
    start_agent_session,
    start_graph_session,
    wait_terminal,
)
from tests._support.smk import smk
from tests._support.testconfig import load_config, requires

pytestmark = pytest.mark.asyncio

# Real LM Studio turns can take 10-60s; tool-calling turns longer. Be generous.
_REAL_TIMEOUT = 180.0


def _real_cfg() -> dict:
    return load_config()["llm"]["real"]


@smk("SMK-AGT-02")
@requires("llm:real")
async def test_real_agent_run_on_workspace(
    authed_client, unique_suffix, tmp_path
):
    """An agent run driven by the real model: start a workspace session with a
    simple instruction, assert it ends successfully and a turn ran with a
    ``stop`` finish (the real model produced output, not an error)."""
    agent = await make_real_agent(authed_client, _real_cfg(), suffix=unique_suffix)
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(
        authed_client,
        workspace_id=wid,
        agent_id=agent["agent_id"],
        instructions="Reply with the single word DONE and nothing else.",
    )
    final = await wait_terminal(authed_client, sid, timeout_s=_REAL_TIMEOUT)
    assert final.get("status") == "ended", final

    tl = await authed_client.get(f"/v1/sessions/{sid}/turn_log")
    assert tl.status_code == 200, tl.text
    items = tl.json()["items"]
    assert items, tl.text
    # The real turn completed normally (stop), not an error finish.
    assert any(i.get("finish_reason") == "stop" for i in items), items
    # No internal-error envelope leaked into the turn log.
    assert not any(
        "internal" in str(i.get("error", "")).lower() for i in items
    ), items


@smk("SMK-GRF-02")
@requires("llm:real")
async def test_real_minimal_graph_run(authed_client, unique_suffix, tmp_path):
    """A minimal begin -> agent -> end graph driven by the real model reaches a
    terminal state and the run-level graph turn log is retrievable."""
    agent = await make_real_agent(authed_client, _real_cfg(), suffix=unique_suffix)
    nodes = [
        {"kind": "begin", "id": "start"},
        {
            "kind": "agent",
            "id": "step",
            "agent_id": agent["agent_id"],
            "input_template": "Reply with the single word OK and nothing else.",
        },
        {"kind": "end", "id": "done", "output_template": "{{ nodes.step.text }}"},
    ]
    edges = [
        {"kind": "static", "from_node": "start", "to_node": "step"},
        {"kind": "static", "from_node": "step", "to_node": "done"},
    ]
    gid = await make_graph(authed_client, suffix=unique_suffix, nodes=nodes, edges=edges)
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_graph_session(authed_client, workspace_id=wid, graph_id=gid)
    final = await wait_terminal(authed_client, sid, timeout_s=_REAL_TIMEOUT)
    assert final.get("status") == "ended", final
    # The run reached the terminal state cleanly (not cancelled/errored).
    assert final.get("ended_reason") == "completed", final

    tl = await authed_client.get(f"/v1/graphs/{gid}/runs/{sid}/turn_log")
    assert tl.status_code == 200, tl.text
    # The graph turn log records superstep orchestration events. Assert the
    # real-model run traversed the graph: the agent ``step`` node and the
    # ``done`` end node both completed, and nothing failed.
    items = tl.json().get("items", [])
    assert items, tl.text
    completed: set[str] = set()
    for ev in items:
        assert not ev.get("failed_node_ids"), ev
        for nid in ev.get("completed_node_ids", []) or []:
            completed.add(nid)
    assert {"step", "done"} <= completed, completed


@smk("SMK-CHT-01")
@requires("llm:real")
async def test_real_chat_turn(authed_client, unique_suffix):
    """A real chat completion turn over the chat WS ends with an assistant
    message. We send one user message and assert the server streams an
    assistant token frame followed by a ``done`` frame with non-empty text."""
    import websockets

    agent = await make_real_agent(authed_client, _real_cfg(), suffix=unique_suffix)
    cleanup = [f"/v1/agents/{agent['agent_id']}", f"/v1/llm_providers/{agent['provider_id']}"]
    cid = None
    try:
        r = await authed_client.post("/v1/chats", json={"agent_id": agent["agent_id"]})
        assert r.status_code in (200, 201), r.text
        cid = r.json()["id"]
        cleanup.insert(0, f"/v1/chats/{cid}")

        http_url = str(authed_client.base_url).rstrip("/")
        ws_origin = http_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_origin}/v1/chats/{cid}/ws"

        # The chat WS is auth-guarded (closes 4401 otherwise). Forward the
        # operator session cookie set on the authed httpx client.
        cookie_hdr = "; ".join(
            f"{c.name}={c.value}" for c in authed_client.cookies.jar
        )
        ws_headers = {"Cookie": cookie_hdr} if cookie_hdr else {}

        assistant_text = ""
        saw_done = False
        stop_reason = None
        async with websockets.connect(
            ws_url, max_size=None, additional_headers=ws_headers
        ) as ws:
            # Spec: initial ``usage`` frame after accept().
            initial = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
            assert initial["kind"] == "usage", initial
            await ws.send(
                json.dumps(
                    {
                        "kind": "user_message",
                        "content": "Reply with the single word DONE and nothing else.",
                    }
                )
            )
            # Drain frames until the turn's ``done`` row arrives (or timeout).
            # assistant_token frames stream the reply in the ``delta`` field;
            # ``done`` carries the stop_reason.
            deadline = asyncio.get_event_loop().time() + _REAL_TIMEOUT
            while not saw_done:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                frame = json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=remaining)
                )
                kind = frame.get("kind")
                if kind == "assistant_token":
                    assistant_text += frame.get("delta") or ""
                elif kind == "done":
                    saw_done = True
                    stop_reason = frame.get("stop_reason")

        assert saw_done, "chat turn did not reach a 'done' frame"
        # The turn completed normally (not an error/length truncation surface).
        assert stop_reason == "stop", f"unexpected stop_reason: {stop_reason!r}"

        # Fall back to the persisted message log if the stream carried no text.
        if not assistant_text.strip():
            mr = await authed_client.get(f"/v1/chats/{cid}/messages")
            assert mr.status_code == 200, mr.text
            body = mr.json()
            msgs = body.get("items", body) if isinstance(body, dict) else body
            for m in msgs or []:
                if m.get("kind") == "assistant_token":
                    assistant_text += str((m.get("payload") or {}).get("delta") or "")
        assert assistant_text.strip(), "real chat turn produced no assistant text"
    finally:
        for url in cleanup:
            try:
                await authed_client.delete(url)
            except Exception:  # noqa: BLE001
                pass
