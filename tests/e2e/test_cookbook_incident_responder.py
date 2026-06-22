"""Cookbook recipe #6 regression: webhook incident responder.

Guards the WEBHOOK -> fresh-session execution hand-off that was silently
broken: a real alert POSTed to the public ``/v1/webhooks/{token}`` endpoint
returns 202, fires an ``agent_fresh_session`` subscription, and the fresh
session must be CREATED, CLAIMED, and RUN TO TERMINAL -- producing a transcript
-- not stranded ``running``/``ended`` with no execution (the recipe's documented
"Known issue" before this fix).

Recipe: primerhq.github.io/docs_source/cookbook/incident-responder.md

The webhook dispatch is fire-and-forget (a FastAPI BackgroundTask returns 202
immediately), so the test polls the workspace sessions for the dispatched row
and then waits for it to run to a terminal, completed state.

The ``payload_template`` renders the raw ``{{ webhook_body }}`` into the agent's
instructions (the recipe's payload-passing surface). The agent is scripted to
triage and call ``misc__inform_user`` ONCE with a concise summary.

HERMETIC delivery assertion: with no channel bound, ``inform_user`` degrades to
``delivered_to: 0`` but the ``tool_call`` is still RECORDED in the on-disk
transcript, so we assert on the transcript's ``misc__inform_user`` call (+ its
``message`` arg). The recipe's live ``delivered_to: 1`` Discord round-trip is
the manual coverage for the channel transport and is gated behind the
``channels:discord`` capability below.

Run with:
    PRIMER_RUN_E2E=1 uv run pytest tests/e2e/test_cookbook_incident_responder.py -n0 -q
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    wait_terminal,
)
from tests._support.smk import smk

pytestmark = [pytest.mark.asyncio]


_TRIAGE_MESSAGE = (
    "payments-api: SEV1, 5xx 40% / p99 8s in us-east. Likely cause: a bad "
    "deploy or an overloaded dependency. First step: roll back the last "
    "release and check the DB connection pool."
)

_ALERT_BODY = json.dumps(
    {
        "service": "payments-api",
        "error": "5xx error rate 40%, p99 latency 8s",
        "region": "us-east",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_webhook_trigger(client, *, slug: str) -> dict:
    r = await client.post(
        "/v1/triggers",
        json={
            "slug": slug,
            "name": f"E2E incident webhook {slug}",
            "config": {"kind": "webhook"},
            "enabled": True,
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _create_agent_fresh_sub(
    client, *, trigger_id: str, agent_id: str, workspace_id: str,
) -> dict:
    r = await client.post(
        f"/v1/triggers/{trigger_id}/subscriptions",
        json={
            "config": {
                "kind": "agent_fresh_session",
                "agent_id": agent_id,
                "workspace_id": workspace_id,
            },
            "payload_template": (
                "Incident alert received: {{ webhook_body }}. Triage it and "
                "post a summary."
            ),
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def _tool_calls(transcript: str) -> list[dict]:
    calls: list[dict] = []
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("role") != "assistant":
            continue
        for part in obj.get("parts", []):
            if part.get("type") == "tool_call":
                calls.append(part)
    return calls


def _inform_user_messages(transcript: str) -> list[str]:
    out: list[str] = []
    for part in _tool_calls(transcript):
        name = part.get("name") or part.get("tool_name") or ""
        if "inform_user" not in name:
            continue
        args = part.get("arguments") or part.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if isinstance(args, dict) and "message" in args:
            out.append(str(args["message"]))
    return out


async def _find_dispatched_session(
    client, workspace_id: str, trigger_id: str, *,
    attempts: int = 40, delay_s: float = 0.5,
) -> str | None:
    """Poll the workspace's sessions for the one this trigger dispatched.

    The webhook returns 202 immediately and dispatches in a BackgroundTask,
    so the session row appears shortly after.
    """
    for _ in range(attempts):
        r = await client.get(
            "/v1/sessions", params={"workspace_id": workspace_id},
        )
        if r.status_code == 200:
            body = r.json()
            items = body.get("items", body) if isinstance(body, dict) else body
            for s in items:
                if (s.get("metadata") or {}).get("trigger_id") == trigger_id:
                    return s["id"]
        await asyncio.sleep(delay_s)
    return None


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@smk("SMK-COOKBOOK-06")
async def test_incident_webhook_triages_and_alerts(
    authed_client, anon_client, mock_llm, unique_suffix, tmp_path,
):
    """POST a real alert to the public webhook -> 202 -> the fresh session is
    created, claimed, and RUNS TO TERMINAL (the regression guard) -> the agent
    triages and records an inform_user summary in the transcript."""
    registry, base_url = mock_llm
    sfx = f"inc-{unique_suffix}"
    cleanup: list[str] = []
    try:
        agent = await make_scripted_agent(
            authed_client, registry, base_url, suffix=sfx,
            scenario=f"scripted:{sfx}",
            tools=["misc__inform_user"],
            system_prompt=[
                "You are an incident responder. An alert payload is in your "
                "input. Triage it, then call inform_user ONCE with a concise "
                "summary (service, severity, likely cause, first step). Stop."
            ],
            rules=[
                Rule(
                    when_tool_result=False,
                    emit_tool="misc__inform_user",
                    emit_args={"message": _TRIAGE_MESSAGE},
                ),
                Rule(when_tool_result=True, emit_text="Triage posted. Done."),
            ],
        )
        wid = await make_local_workspace(authed_client, suffix=sfx, root=tmp_path)

        trigger = await _create_webhook_trigger(
            authed_client, slug=f"e2e-inc-{unique_suffix}",
        )
        cleanup.append(f"/v1/triggers/{trigger['id']}")
        token = trigger["config"]["token"]
        await _create_agent_fresh_sub(
            authed_client, trigger_id=trigger["id"],
            agent_id=agent["agent_id"], workspace_id=wid,
        )

        # POST a real alert to the PUBLIC, unauthenticated webhook endpoint.
        r = await anon_client.post(
            f"/v1/webhooks/{token}",
            content=_ALERT_BODY.encode("utf-8"),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 202, r.text
        assert r.json()["status"] == "accepted"

        # The dispatch is fire-and-forget; find the session it created.
        sid = await _find_dispatched_session(authed_client, wid, trigger["id"])
        assert sid is not None, (
            "webhook fired but no agent_fresh_session was dispatched into the "
            "workspace (the dispatch path is dead)"
        )

        # REGRESSION GUARD: the fired session must RUN TO TERMINAL, not hang
        # or strand. Before the slot-allocation fix it ended with no
        # transcript / sat unstarted; it must now complete the agent turn.
        final = await wait_terminal(authed_client, sid, timeout_s=120)
        assert final.get("status") == "ended", (
            f"webhook-fired session did not reach a terminal state (it hung -- "
            f"the regression this guards): {final}"
        )
        assert final.get("ended_reason") == "completed", (
            f"webhook-fired session did not run to completion: {final}"
        )

        msgs = (
            tmp_path / wid / ".state" / "sessions" / sid / "messages.jsonl"
        )
        assert msgs.exists(), (
            f"webhook-fired session wrote no transcript at {msgs} -- the "
            f"on-disk slot was not allocated (the bug this fix addresses)"
        )
        transcript = msgs.read_text(encoding="utf-8")

        # The webhook body was rendered into the instructions and the agent
        # triaged + recorded the inform_user summary.
        assert "payments-api" in transcript, (
            "webhook_body was not rendered into the session instructions"
        )
        informs = _inform_user_messages(transcript)
        assert informs, (
            f"responder did not record a misc__inform_user triage: {transcript!r}"
        )
        assert _TRIAGE_MESSAGE in informs, (
            f"triage summary not found in inform_user calls: {informs!r}"
        )
    finally:
        for url in reversed(cleanup):
            await authed_client.delete(url)
