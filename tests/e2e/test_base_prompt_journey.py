"""CI-owned journey (Wiki First W4): the launch-time base prompt survives compaction and resume.

Modeled on ``test_ask_user_resume_cycle_journey.py``: a scripted mock-LLM
agent, the real engine path, no DB injection. The base prompt is set at
launch and must hold in TWO senses, at three stages:

  * WIRE BYTE IDENTITY: the full system message the model received starts
    with the base prompt byte-identical (``head.startswith(BASE + "\\n\\n")``)
    on the launch turn, the resumed turn and the post-compaction turn.
  * BEHAVIOUR: the scripted agent FOLLOWED the base prompt - its transcript
    carries the token answer it was instructed to give (the mock emits
    TOKEN only because the base prompt tells it to), before and after the
    compaction fold.

The compaction itself runs the REAL compactor against the session's own
LLM (``POST .../compact``), whose captured request (prompt + serialised
history) must never contain the base prompt, since it lives in the system
slot the compactor never sees.

The mock registry (in the test process) captures every chat request the
server makes, so the wire assertions read the full system message as the
model received it.

Auto-ignored unless ``PRIMER_RUN_E2E=1`` (the e2e conftest); CI runs it
against the bringup server.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
    wait_completed,
)
from tests._support.yield_journeys import wait_for_resume

BASE = "ALWAYS: answer the token question with the token TOKEN-42."
TOKEN = "TOKEN-42"
QUESTION = "what is the token?"


def _scenario_requests(registry, scenario: str) -> list[dict]:
    return [req for req in registry.requests if req.get("model") == scenario]


def _system_head(req: dict) -> str:
    for m in req.get("messages") or []:
        if m.get("role") == "system":
            return str(m.get("content") or "")
    raise AssertionError(f"no system message in the captured request: {req!r}")


async def _wait_for_new_request(registry, scenario: str, before: int, timeout_s: float = 60.0) -> dict:
    """Poll until a scenario request lands past ``before`` (count captured
    BEFORE the step that drives it)."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        reqs = _scenario_requests(registry, scenario)
        if len(reqs) > before:
            return reqs[-1]
        await asyncio.sleep(0.5)
    raise AssertionError(f"no new model request landed within {timeout_s}s (scenario {scenario!r})")


async def _wait_for_token_answers(client: httpx.AsyncClient, sid: str, count: int, timeout_s: float = 60.0) -> int:
    """Poll the transcript until it carries at least ``count`` assistant
    answers holding the token (the behaviour: the agent followed the base
    prompt). Returns the answer count."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        items = (await client.get(f"/v1/sessions/{sid}/messages", params={"limit": 500})).json()["items"]
        answers = [
            r.get("payload", {}).get("text", "")
            for r in items
            if r.get("kind") == "assistant_token"
        ]
        n = sum(1 for t in answers if TOKEN in str(t))
        if n >= count:
            return n
        await asyncio.sleep(0.5)
    raise AssertionError(f"session {sid} never answered the token {count}x: {answers!r}")


@pytest.mark.asyncio
async def test_the_base_prompt_survives_compaction_and_resume(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path,
) -> None:
    registry, base_url = mock_llm
    scenario = f"scripted:{unique_suffix}"

    # Rule order matters (first match wins): the post-compaction steer turn
    # (its user message names the "second token") gets the terminating text
    # answer; the launch turn (ask_user offered, no tool result yet) offers
    # the park; the resume after the park (a tool result present) answers
    # with the token. The compactor's request matches none of the three -
    # no tools are offered to the summariser and its history is serialised
    # text, so it takes the registry's permissive default.
    agent = await make_scripted_agent(
        client, registry, base_url,
        suffix=unique_suffix, scenario=scenario,
        tools=["system__ask_user"],
        rules=[
            Rule(when_last_user_contains="second token", emit_text=TOKEN),
            Rule(when_tool_offered="ask_user", when_tool_result=False,
                 emit_tool="system__ask_user", emit_args={"prompt": QUESTION}),
            Rule(when_tool_result=True, emit_text=TOKEN),
        ],
    )
    wid = await make_local_workspace(client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(
        client, workspace_id=wid, agent_id=agent["agent_id"],
        instructions=QUESTION, base_prompt=BASE,
    )

    # ----- 1. The launch turn parks on ask_user; its request carries the
    #         base prompt byte-identical at the head ---------------------
    deadline = asyncio.get_event_loop().time() + 60.0
    parked: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/sessions/{sid}")
        if r.status_code == 200:
            parked = r.json()
            if parked.get("parked_status") == "parked":
                break
            if parked.get("status") in ("ended",) or parked.get("session_state") == "parked":
                raise AssertionError(
                    f"session {sid} finished before parking on ask_user: {parked!r}"
                )
        await asyncio.sleep(0.5)
    assert parked.get("parked_status") == "parked", f"never parked: {parked!r}"
    initial_turn_no = parked["turn_no"]

    reqs = _scenario_requests(registry, scenario)
    assert reqs, "no model request captured for the launch turn"
    head = _system_head(reqs[0])
    assert head.startswith(BASE + "\n\n"), f"launch turn lost the base prompt head: {head[:120]!r}"

    # ----- 2. Operator answers; the engine resumes the session ----------
    # The resumed model request is driven by THIS respond, so the count is
    # captured BEFORE the POST: waiting for "> after" would wait forever.
    n_before_resume = len(_scenario_requests(registry, scenario))
    r = await client.get(f"/v1/sessions/{sid}/ask_user/pending")
    assert r.status_code == 200, r.text
    pending = r.json()
    r = await client.post(
        f"/v1/sessions/{sid}/ask_user/respond",
        json={"tool_call_id": pending["tool_call_id"], "response": "the token, as instructed"},
    )
    assert r.status_code == 202, r.text
    body = await wait_for_resume(client, sid, min_turn_no=initial_turn_no + 1)
    assert body["parked_status"] is None, body

    # ----- 3. The resumed turn: wire head leads with the base prompt, and
    #         the agent BEHAVED per it - the transcript carries the token
    #         answer it was instructed to give ---------------------------
    resumed_req = await _wait_for_new_request(registry, scenario, n_before_resume)
    head = _system_head(resumed_req)
    assert head.startswith(BASE + "\n\n"), f"resumed turn lost the base prompt head: {head[:120]!r}"
    await wait_completed(client, sid)
    assert await _wait_for_token_answers(client, sid, 1) >= 1, "the agent never answered the token"

    # ----- 4. A REAL compaction: the compactor's call never carries the
    #         base prompt (it lives in the system slot the compactor
    #         never sees) -----------------------------------------------
    n_before_compact = len(_scenario_requests(registry, scenario))
    r = await client.post(f"/v1/workspaces/{wid}/sessions/{sid}/compact")
    assert r.status_code == 200, r.text
    new_reqs = _scenario_requests(registry, scenario)
    assert len(new_reqs) > n_before_compact, "the compactor never made its LLM call"
    compactor_req = new_reqs[-1]
    assert BASE not in json.dumps(compactor_req.get("messages", [])), (
        "the compactor's input (prompt + serialised history) held the base prompt"
    )
    assert not _system_head(compactor_req).startswith(BASE), (
        "the compactor's own system message is not the compaction prompt"
    )

    # ----- 5. The post-compaction turn: wire head leads with the base
    #         prompt byte-identical; the behaviour holds (a second token
    #         answer); and the pre-compaction answer is still in the
    #         (folded) history the model got ------------------------------
    n_before_post = len(_scenario_requests(registry, scenario))
    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/{sid}/steer",
        json={"instruction": "what is the second token?"},
    )
    assert r.status_code == 200, r.text
    post_req = await _wait_for_new_request(registry, scenario, n_before_post)
    head = _system_head(post_req)
    assert head.startswith(BASE + "\n\n"), f"post-compaction turn lost the base prompt head: {head[:120]!r}"
    # The token answer the resume turn produced is still there, so this is
    # the same session history (folded by the marker), not a fresh one.
    assistant_texts = [
        str(m.get("content") or "") for m in post_req.get("messages") or []
        if m.get("role") == "assistant"
    ]
    assert any(TOKEN in t for t in assistant_texts), (
        f"the pre-compaction answer is gone from the post-compaction history: {assistant_texts!r}"
    )
    await wait_completed(client, sid)
    assert await _wait_for_token_answers(client, sid, 2) >= 2, (
        "the post-compaction agent did not follow the base prompt a second time"
    )
