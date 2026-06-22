"""Cookbook recipe #4 regression: scheduled stock-news monitor.

Pins the AUTOMATABLE core of the stock-monitor recipe and, crucially, guards
the trigger-fired fresh-session execution path that was silently broken: a
``scheduled`` trigger with an ``agent_fresh_session`` subscription, fired via
``POST /v1/triggers/{id}/fire_now``, must spin up a brand-new agent session
that ACTUALLY RUNS TO TERMINAL (allocates its on-disk slot, claims, executes
the agent turn) -- not a row that silently flips to ``ended`` with no
transcript.

Recipe: primerhq.github.io/docs_source/cookbook/scheduled-stock-monitor.md

The recipe's "judge material news, then alert" behaviour is scripted with the
deterministic mock LLM:

  * material path -- the agent judges the news material and calls
    ``misc__inform_user`` ONCE with a one-line alert, then stops; and
  * silent path -- the agent judges nothing material and ends WITHOUT calling
    ``inform_user`` (no noise on slow news days).

HERMETIC delivery assertion: with no channel bound to the workspace the
``inform_user`` call degrades to ``delivered_to: 0`` but the ``tool_call`` is
still RECORDED in the on-disk transcript, so we assert on the transcript's
``misc__inform_user`` tool_call (+ its ``message`` arg) rather than a live
channel post. The recipe's live ``delivered_to: 1`` Discord round-trip is the
manual coverage for the channel transport and is out of scope here.

The web-search leg of the recipe (``web__web_search``) is folded into the
instruction text so the test stays hermetic (no real web dependency); the
judgement + alert behaviour the recipe is about is fully exercised.

Run with:
    PRIMER_RUN_E2E=1 uv run pytest tests/e2e/test_cookbook_stock_monitor.py -n0 -q
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


_ALERT_MESSAGE = (
    "ALERT NVDA: regulator opened an antitrust probe into its data-center "
    "GPU sales -- likely to move the stock."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_scheduled_trigger(client, *, slug: str) -> dict:
    """A scheduled trigger fixed far in the future so the scheduler never
    fires it on its own; the test drives it via fire_now."""
    r = await client.post(
        "/v1/triggers",
        json={
            "slug": slug,
            "name": f"E2E stock monitor {slug}",
            "config": {
                "kind": "scheduled",
                "cron": "0 0 1 1 *",  # 00:00 on Jan 1 -- effectively never
                "timezone": "UTC",
                "catchup": "none",
            },
            "enabled": True,
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _create_agent_fresh_sub(
    client, *, trigger_id: str, agent_id: str, workspace_id: str, payload: str,
) -> dict:
    r = await client.post(
        f"/v1/triggers/{trigger_id}/subscriptions",
        json={
            "config": {
                "kind": "agent_fresh_session",
                "agent_id": agent_id,
                "workspace_id": workspace_id,
            },
            "payload_template": payload,
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def _dispatched_session_id(fire_result: dict) -> str | None:
    for res in fire_result.get("results", []):
        if res.get("ok") and res.get("artefact_id"):
            return res["artefact_id"]
    return None


def _tool_calls(transcript: str) -> list[dict]:
    """Return every assistant tool_call part recorded in the transcript."""
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
    """Extract the ``message`` arg of every misc__inform_user tool_call."""
    out: list[str] = []
    for part in _tool_calls(transcript):
        name = part.get("tool_name") or part.get("name") or ""
        if "inform_user" not in name:
            continue
        args = part.get("args") or part.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if isinstance(args, dict) and "message" in args:
            out.append(str(args["message"]))
    return out


async def _fire_and_wait(
    client, trigger_id: str, workspace_id: str, tmp_path,
) -> tuple[dict, str]:
    """fire_now the trigger, resolve the dispatched session, poll to terminal,
    and return (final_session, transcript_text)."""
    r = await client.post(f"/v1/triggers/{trigger_id}/fire_now", json={})
    assert r.status_code == 200, r.text
    fire = r.json()
    assert not fire.get("skipped"), fire
    sid = _dispatched_session_id(fire)
    assert sid is not None, f"fire_now did not dispatch a session: {fire}"

    final = await wait_terminal(client, sid, timeout_s=120)
    # The regression guard: a trigger-fired fresh session must RUN, not
    # silently strand. Before the slot-allocation fix it ended with no
    # transcript; now it completes the agent turn.
    assert final.get("status") == "ended", final
    assert final.get("ended_reason") == "completed", (
        f"trigger-fired session did not run to completion (the slot-allocation "
        f"regression guard): {final}"
    )

    msgs = tmp_path / workspace_id / ".state" / "sessions" / sid / "messages.jsonl"
    assert msgs.exists(), (
        f"trigger-fired session wrote no on-disk transcript at {msgs} -- the "
        f"on-disk slot was not allocated (the bug this fix addresses)"
    )
    return final, msgs.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@smk("SMK-COOKBOOK-04")
async def test_monitor_alerts_on_material_news(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    """Material news -> the scheduled trigger fires a fresh session that RUNS
    to terminal, the agent judges it material, and an inform_user alert
    tool_call is recorded in the transcript."""
    registry, base_url = mock_llm
    sfx = f"stk-mat-{unique_suffix}"
    cleanup: list[str] = []
    try:
        # Scripted monitor: no prior tool result -> alert (material);
        # after the inform_user result comes back -> a terse closing line.
        agent = await make_scripted_agent(
            authed_client, registry, base_url, suffix=sfx,
            scenario=f"scripted:{sfx}",
            tools=["misc__inform_user"],
            system_prompt=[
                "You monitor stock news. Judge whether the news in your input "
                "is materially impactful. If material, call inform_user ONCE "
                "with a one-line alert; otherwise do not call it. Then stop."
            ],
            rules=[
                Rule(
                    when_tool_result=False,
                    emit_tool="misc__inform_user",
                    emit_args={"message": _ALERT_MESSAGE},
                ),
                Rule(when_tool_result=True, emit_text="Alert sent. Done."),
            ],
        )
        wid = await make_local_workspace(authed_client, suffix=sfx, root=tmp_path)

        trigger = await _create_scheduled_trigger(
            authed_client, slug=f"e2e-stk-mat-{unique_suffix}",
        )
        cleanup.append(f"/v1/triggers/{trigger['id']}")
        await _create_agent_fresh_sub(
            authed_client, trigger_id=trigger["id"], agent_id=agent["agent_id"],
            workspace_id=wid,
            payload=(
                "Check these tickers for material news and alert if material: "
                "NVDA. News: a regulator opened an antitrust probe into NVDA's "
                "data-center GPU sales."
            ),
        )

        _final, transcript = await _fire_and_wait(
            authed_client, trigger["id"], wid, tmp_path,
        )

        # The agent judged material and recorded the inform_user alert.
        msgs = _inform_user_messages(transcript)
        assert msgs, (
            f"material path did not record a misc__inform_user tool_call: "
            f"{transcript!r}"
        )
        assert _ALERT_MESSAGE in msgs, (
            f"inform_user alert message not found in transcript calls: {msgs!r}"
        )
    finally:
        for url in reversed(cleanup):
            await authed_client.delete(url)


@smk("SMK-COOKBOOK-04")
async def test_monitor_silent_on_no_news(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    """No material news -> the fired session still RUNS to terminal, but the
    agent stays silent (NO inform_user tool_call) -- the filtering the recipe
    is about."""
    registry, base_url = mock_llm
    sfx = f"stk-sil-{unique_suffix}"
    cleanup: list[str] = []
    try:
        # Scripted monitor: judges nothing material -> a plain text reply, no
        # tool call at all.
        agent = await make_scripted_agent(
            authed_client, registry, base_url, suffix=sfx,
            scenario=f"scripted:{sfx}",
            tools=["misc__inform_user"],
            system_prompt=[
                "You monitor stock news. If nothing is materially impactful, "
                "do not call inform_user; just stop."
            ],
            rules=[
                Rule(
                    when_tool_result=False,
                    emit_text="Reviewed the news; nothing material today.",
                ),
            ],
        )
        wid = await make_local_workspace(authed_client, suffix=sfx, root=tmp_path)

        trigger = await _create_scheduled_trigger(
            authed_client, slug=f"e2e-stk-sil-{unique_suffix}",
        )
        cleanup.append(f"/v1/triggers/{trigger['id']}")
        await _create_agent_fresh_sub(
            authed_client, trigger_id=trigger["id"], agent_id=agent["agent_id"],
            workspace_id=wid,
            payload=(
                "Check these tickers for material news and alert if material: "
                "TSLA. News: a minor blog reposted last quarter's known "
                "delivery figures; nothing new."
            ),
        )

        _final, transcript = await _fire_and_wait(
            authed_client, trigger["id"], wid, tmp_path,
        )

        # No inform_user call on the silent path.
        assert "inform_user" not in transcript, (
            f"silent path unexpectedly called inform_user: {transcript!r}"
        )
        assert not _inform_user_messages(transcript)
    finally:
        for url in reversed(cleanup):
            await authed_client.delete(url)
