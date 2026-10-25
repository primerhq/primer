"""The session canvas over HTTP, live (pivot P5 S6: the frame route, the file capability URLs and the canvas REST routes; S10 adds the agent journeys).

LEAD-SCHEDULED: never run locally (AGENTS.md section 5); the implementer only collects and lints this file.

The canvas toolset is on by default now that the canvas panel ships (S8), so an agent can draw it. Two journeys grant an agent the canvas
tools and drive a full turn: the agent applies a drawing and reads the user's value back, and the agent parks on ``canvas_wait`` for a
matching click. The others draw as an operator (``POST .../canvas/ops``) and click as a person (``POST .../canvas/events``): the agent
needs no canvas tool to be woken by a click.

1. An agent applies the playlist with ``canvas__canvas_apply`` and, woken by a click, reads the user's value with ``canvas__canvas_get``:
   the ``<canvas_event>`` block carries the value and the event feed lists both events.
2. An agent parks on ``canvas_wait`` for a matching click: a click on the other element is ``queued`` and, after the turn ends, is
   delivered as the next instruction.
3. An operator draws, a Select change is only recorded, and a click wakes the idle session: the agent's next instruction is the
   ``<canvas_event>`` block, which carries the user's current values, and the event feed lists both events.
4. An ended session refuses a click with 409 ``session_ended`` and records nothing.
5. The frame shell carries the design's policy (with the server's own http origin), and a workspace file an Image shows is served by
   its capability URL, cross-origin, until the Image is deleted.
"""
from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from tests._support.default_workspace import default_workspace_id
from tests._support.mock_llm import Rule
from tests._support.model_profiles import profile_id_for

pytestmark = pytest.mark.asyncio

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

DRAWING = """
root = Column([controls])
controls = Row([genre, play])
genre = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")
play = Button("Play", "primary")
"""

# Two Buttons (journey 2): the agent waits only for ``ok``; a click on ``cancel`` is not the answer, so it is queued.
OK_CANCEL = """
root = Column([ok, cancel])
ok = Button("OK", "primary")
cancel = Button("Cancel")
"""


async def _messages(client: httpx.AsyncClient, session_id: str) -> list[dict]:
    r = await client.get(f"/v1/sessions/{session_id}/messages", params={"limit": 500})
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def _wait_for(client: httpx.AsyncClient, session_id: str, needle: str, *, timeout_s: float = 120.0) -> list[dict]:
    """The transcript once one of the AGENT's records holds ``needle`` (an instruction that quotes it does not count)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        items = await _messages(client, session_id)
        if needle in json.dumps([rec for rec in items if rec.get("kind") != "user_input"]):
            return items
        await asyncio.sleep(0.5)
    pytest.fail(f"{needle!r} never reached the transcript of {session_id}")


async def _agent(client: httpx.AsyncClient, mock_llm, suffix: str, rules: list[Rule], tools: list[str] | None = None) -> str:
    """A scripted agent on its own provider and profile; ``tools`` (the canvas tools, for the agent journeys) default to none. Returns its id."""
    registry, mock_url = mock_llm
    model = f"canvas-{suffix}"
    provider_id = f"llm-canvas-{suffix}"
    profile = profile_id_for(provider_id, model)
    registry.register(model, rules)
    r = await client.post("/v1/llm_providers", json={
        "id": provider_id, "provider": "openchat", "config": {"url": mock_url, "flavor": "lmstudio"}, "limits": {"max_concurrency": 4},
    })
    assert r.status_code in (200, 201), r.text
    r = await client.post("/v1/model_profiles", json={
        "id": profile, "description": "e2e canvas profile", "provider_id": provider_id, "model_name": model, "context_length": 32000,
    })
    assert r.status_code in (200, 201), r.text
    agent_id = f"canvas-agent-{suffix}"
    r = await client.post("/v1/agents", json={
        "id": agent_id, "description": "Answers the user's canvas clicks.", "model": {"profile_id": profile}, "tools": tools or [],
        "prompt": "Answer each message in one line.",
    })
    assert r.status_code in (200, 201), r.text
    return agent_id


async def _session(client: httpx.AsyncClient, wid: str, agent_id: str) -> str:
    r = await client.post(f"/v1/workspaces/{wid}/sessions", json={"binding": {"kind": "agent", "agent_id": agent_id}})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _wait_for_park(client: httpx.AsyncClient, session_id: str, *, timeout_s: float = 120.0) -> dict:
    """The session row once it is parked (a ``canvas_wait`` holds the turn)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        r = await client.get(f"/v1/sessions/{session_id}")
        if r.status_code == 200 and r.json().get("parked_status"):
            return r.json()
        await asyncio.sleep(0.5)
    pytest.fail(f"session {session_id} never parked")


async def test_an_operator_draws_and_a_click_wakes_the_agent(client: httpx.AsyncClient, mock_llm, unique_suffix: str) -> None:
    ready, saw = f"READY-{unique_suffix}", f"SAW-CLICK-{unique_suffix}"
    agent_id = await _agent(client, mock_llm, unique_suffix, [
        Rule(when_last_user_contains="<canvas_event", emit_text=f"{saw} done"),
        Rule(when_last_user_contains=f"hello-{unique_suffix}", emit_text=f"{ready} waiting"),
    ])
    try:
        wid = await default_workspace_id(client)
        sid = await _session(client, wid, agent_id)
        r = await client.post(f"/v1/workspaces/{wid}/sessions/{sid}/steer", json={"instruction": f"hello-{unique_suffix}"})
        assert r.status_code in (200, 201, 202), r.text
        await _wait_for(client, sid, ready)

        canvas = f"/v1/workspaces/{wid}/sessions/{sid}/canvas"
        r = await client.post(f"{canvas}/ops", json={"source": DRAWING})
        assert r.status_code == 200 and r.json()["rev"] == 1, r.text
        r = await client.post(f"{canvas}/events", json={"element": "genre", "kind": "change", "value": "Lo-fi"})
        assert r.status_code == 202 and r.json()["delivery"] == "recorded", r.text
        # The session may still be settling its first turn: a click then waits behind it (queued) and is delivered when the turn ends.
        r = await client.post(f"{canvas}/events", json={"element": "play", "kind": "click"})
        assert r.status_code == 202 and r.json()["delivery"] in ("woken", "queued"), r.text

        items = await _wait_for(client, sid, saw)
        [clicked] = [
            rec for rec in items
            if rec.get("kind") == "user_input" and (rec.get("payload") or {}).get("canvas_event", {}).get("element") == "play"
        ]
        assert clicked["payload"]["text"].startswith("<canvas_event")
        assert 'values: genre="Lo-fi"' in clicked["payload"]["text"]
        assert "value" not in clicked["payload"]["canvas_event"], "the structured copy carries no input"

        view = (await client.get(canvas)).json()
        assert view["values"] == {"genre": "Lo-fi"} and [e["kind"] for e in view["events"]] == ["change", "click"]
        events = await client.get("/v1/events", params={"event_type": "canvas.event", "workspace_id": wid})
        assert events.status_code == 200, events.text
        mine = [e for e in events.json()["items"] if e.get("session_id") == sid]
        assert sorted(e["payload"]["kind"] for e in mine) == ["change", "click"]
    finally:
        await client.delete(f"/v1/agents/{agent_id}")


async def test_an_agent_draws_and_a_click_wakes_it(client: httpx.AsyncClient, mock_llm, unique_suffix: str) -> None:
    """An agent applies the playlist with ``canvas__canvas_apply``; a click wakes it and it reads the value with ``canvas__canvas_get``."""
    drawn = f"DRAWN-{unique_suffix}"
    saw = f"SAW-LOFI-{unique_suffix}"
    steer = f"draw-playlist-{unique_suffix}"
    agent_id = await _agent(
        client, mock_llm, unique_suffix,
        [
            Rule(when_tool_result=False, when_last_user_contains=steer, emit_tool="canvas__canvas_apply", emit_args={"source": DRAWING}),
            Rule(when_tool_result=True, when_last_user_contains=steer, emit_text=drawn),
            # The tool result is the JSON-dumped ``canvas_get`` result, so the decompiled line's quotes are escaped in
            # the content the mock matches; the needle must carry the backslashes.
            Rule(when_last_tool_result_contains='genre.value = \\"Lo-fi\\"  # user', emit_text=saw),
            Rule(when_last_user_contains="<canvas_event", emit_tool="canvas__canvas_get"),
        ],
        tools=["canvas__canvas_apply", "canvas__canvas_get", "canvas__canvas_wait"],
    )
    try:
        wid = await default_workspace_id(client)
        sid = await _session(client, wid, agent_id)
        r = await client.post(f"/v1/workspaces/{wid}/sessions/{sid}/steer", json={"instruction": steer})
        assert r.status_code in (200, 201, 202), r.text
        await _wait_for(client, sid, drawn)
        canvas = f"/v1/workspaces/{wid}/sessions/{sid}/canvas"
        view = (await client.get(canvas)).json()
        assert "genre" in view["elements"] and "play" in view["elements"], "the agent's canvas_apply drew the playlist"
        # The turn is over, so a change is only recorded and the click wakes.
        r = await client.post(f"{canvas}/events", json={"element": "genre", "kind": "change", "value": "Lo-fi"})
        assert r.status_code == 202 and r.json()["delivery"] == "recorded", r.text
        r = await client.post(f"{canvas}/events", json={"element": "play", "kind": "click"})
        assert r.status_code == 202 and r.json()["delivery"] == "woken", r.text
        items = await _wait_for(client, sid, saw)
        [clicked] = [
            rec for rec in items
            if rec.get("kind") == "user_input" and (rec.get("payload") or {}).get("canvas_event", {}).get("element") == "play"
        ]
        assert clicked["payload"]["text"].startswith("<canvas_event")
        assert 'values: genre="Lo-fi"' in clicked["payload"]["text"]
        events = await client.get("/v1/events", params={"event_type": "canvas.event", "workspace_id": wid})
        assert events.status_code == 200, events.text
        mine = [e for e in events.json()["items"] if e.get("session_id") == sid]
        assert sorted(e["payload"]["kind"] for e in mine) == ["change", "click"]
    finally:
        await client.delete(f"/v1/agents/{agent_id}")


async def test_canvas_wait_resumes_on_a_matching_click_and_queues_the_other(client: httpx.AsyncClient, mock_llm, unique_suffix: str) -> None:
    """An agent parks on ``canvas_wait`` for ``ok``; a click on ``cancel`` is queued and arrives as the next instruction after the turn ends."""
    ok_reply = f"OK-{unique_suffix}"
    cancel_seen = f"CANCEL-SEEN-{unique_suffix}"
    steer = f"wait-journey-{unique_suffix}"
    agent_id = await _agent(
        client, mock_llm, unique_suffix,
        [
            Rule(when_tool_result=False, when_last_user_contains=steer, emit_tool="canvas__canvas_apply", emit_args={"source": OK_CANCEL}),
            Rule(when_last_user_contains=steer, when_last_tool_result_contains='"changed"', emit_tool="canvas__canvas_wait", emit_args={"ids": ["ok"]}),
            Rule(when_last_user_contains=steer, when_last_tool_result_contains='"element": "ok"', emit_text=ok_reply),
            Rule(when_last_user_contains="<canvas_event", emit_text=cancel_seen),
        ],
        tools=["canvas__canvas_apply", "canvas__canvas_get", "canvas__canvas_wait"],
    )
    try:
        wid = await default_workspace_id(client)
        sid = await _session(client, wid, agent_id)
        r = await client.post(f"/v1/workspaces/{wid}/sessions/{sid}/steer", json={"instruction": steer})
        assert r.status_code in (200, 201, 202), r.text
        canvas = f"/v1/workspaces/{wid}/sessions/{sid}/canvas"
        # The agent drew ok and cancel and is parked waiting for a click on ``ok``.
        await _wait_for_park(client, sid)
        # A click on the element the wait does not name is queued, not delivered now.
        r = await client.post(f"{canvas}/events", json={"element": "cancel", "kind": "click"})
        assert r.status_code == 202 and r.json()["delivery"] == "queued", r.text
        # A click on the waited-for element ends the wait and resumes the turn.
        r = await client.post(f"{canvas}/events", json={"element": "ok", "kind": "click"})
        assert r.status_code == 202 and r.json()["delivery"] == "wait", r.text
        # The wait's tool result names the click that ended it.
        items = await _wait_for(client, sid, ok_reply)
        agent_recs = [rec for rec in items if rec.get("kind") != "user_input"]
        assert any('"element": "ok"' in json.dumps(rec) for rec in agent_recs), "the wait's result names the click that ended it"
        # The queued cancel arrives only after the turn ends, and is seen second.
        items = await _wait_for(client, sid, cancel_seen)
        texts = [json.dumps(rec) for rec in items if rec.get("kind") != "user_input"]
        idx_ok = next(i for i, t in enumerate(texts) if ok_reply in t)
        idx_cancel = next(i for i, t in enumerate(texts) if cancel_seen in t)
        assert idx_ok < idx_cancel, "the matching click is seen before the queued one"
    finally:
        await client.delete(f"/v1/agents/{agent_id}")


async def test_an_ended_session_refuses_clicks(client: httpx.AsyncClient, mock_llm, unique_suffix: str) -> None:
    agent_id = await _agent(client, mock_llm, unique_suffix, [Rule(emit_text="ok")])
    try:
        wid = await default_workspace_id(client)
        sid = await _session(client, wid, agent_id)
        canvas = f"/v1/workspaces/{wid}/sessions/{sid}/canvas"
        assert (await client.post(f"{canvas}/ops", json={"source": DRAWING})).status_code == 200
        r = await client.post(f"/v1/workspaces/{wid}/sessions/{sid}/cancel", json={})
        assert r.status_code == 200, r.text

        r = await client.post(f"{canvas}/events", json={"element": "play", "kind": "click"})
        assert r.status_code == 409, r.text
        assert r.json()["extensions"]["code"] == "session_ended"
        assert (await client.get(canvas)).json()["event_seq"] == 0
    finally:
        await client.delete(f"/v1/agents/{agent_id}")


async def test_the_frame_and_file_routes(client: httpx.AsyncClient, anon_client: httpx.AsyncClient, base_url: str, mock_llm, unique_suffix: str) -> None:
    agent_id = await _agent(client, mock_llm, unique_suffix, [Rule(emit_text="ok")])
    path = f"canvas-e2e/{unique_suffix}/c.png"
    wid = await default_workspace_id(client)
    try:
        sid = await _session(client, wid, agent_id)
        canvas = f"/v1/workspaces/{wid}/sessions/{sid}/canvas"

        view = (await client.get(canvas)).json()
        frame = await anon_client.get(view["frame_url"])
        assert frame.status_code == 200 and frame.headers["content-type"].startswith("text/html")
        csp = frame.headers["content-security-policy"]
        assert csp.startswith("sandbox allow-scripts allow-forms; default-src 'none'; ")
        assert csp.endswith("frame-ancestors 'self'") and "connect-src 'none'" in csp
        host = httpx.URL(base_url).netloc.decode()
        assert f"img-src https: data: blob: http://{host};" in csp, "a plain-http install's own origin loads file URLs"
        assert frame.headers["x-frame-options"] == "SAMEORIGIN" and frame.headers["referrer-policy"] == "no-referrer"
        assert frame.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert b"allow-same-origin" not in frame.content

        r = await client.put(
            f"/v1/workspaces/{wid}/files", params={"path": path},
            json={"content": base64.b64encode(PNG).decode(), "encoding": "base64"},
        )
        assert r.status_code == 204, r.text
        r = await client.post(f"{canvas}/ops", json={"source": f'root = Column([c])\nc = Image("file:{path}", "c")'})
        assert r.status_code == 200, r.text
        url = (await client.get(canvas)).json()["file_urls"][f"file:{path}"]

        served = await anon_client.get(url)                      # no cookie: the URL is the credential
        assert served.status_code == 200 and served.content == PNG
        assert served.headers["content-type"] == "image/png"
        assert served.headers["cross-origin-resource-policy"] == "cross-origin"
        assert served.headers["content-security-policy"] == "default-src 'none'; sandbox"

        assert (await client.post(f"{canvas}/ops", json={"source": "del c"})).status_code == 200
        assert (await anon_client.get(url)).status_code == 404, "a file the canvas no longer shows is not served"
        tampered = url[:-2] + ("AA" if not url.endswith("AA") else "BB")
        assert (await anon_client.get(tampered)).status_code == 404
    finally:
        await client.delete(f"/v1/workspaces/{wid}/files", params={"path": f"canvas-e2e/{unique_suffix}", "recursive": "true"})
        await client.delete(f"/v1/agents/{agent_id}")
