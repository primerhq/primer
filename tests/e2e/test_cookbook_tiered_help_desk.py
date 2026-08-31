"""Cookbook recipe #13 regression: Tiered Help Desk with Supervisor Sign-off.

Guards the mechanic this recipe is ABOUT and nothing else covers: the
tiered handoff. A front-line agent takes the customer's question, decides
it belongs to a specialist, and hands the conversation over. The rest of
the conversation runs under the specialist, and the history it inherits is
the SAME history, because a handoff is a binding switch on one session
rather than a new conversation.

Recipe: primerhq.github.io/docs_source/cookbook/tiered-help-desk.md

This module replaces the chat-driven original, which drove the recipe over
``WS /v1/chats/{id}/ws`` and pinned four chat mechanics. Three of those have
session successors already covered elsewhere, so re-asserting them here
would be duplication rather than coverage:

  * KB-grounded answers: ``test_cookbook_support_desk``;
  * ``ask_user`` park and resume, and BOTH tool-approval verdicts (the
    supervisor sign-off): ``test_cookbook_release_conductor``.

The fourth, the handoff, was chat-only and had no session equivalent until
``switch_to_agent`` was ported onto the session binding switch. That port is
what this module guards end to end.

Two properties matter and both are asserted from the server's own state
rather than from the model's output:

1. The switch is QUEUED by the tool and applied at the turn boundary, not
   mid-turn. A binding that changed underneath a running turn would hand
   the front-line agent's own tool results to the specialist.
2. The session keeps its identity across the switch: same session id, same
   transcript, a bumped ``binding_epoch``, and an agent marker in the log
   recording who handed off to whom.

Agent behaviour is scripted (deterministic mock LLM). The handoff DECISION
is the front-line agent's, which is the recipe's premise; the switch itself
is the platform's.

Run with:
    PRIMER_RUN_E2E=1 uv run pytest tests/e2e/test_cookbook_tiered_help_desk.py -n0 -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
    wait_completed,
)
from tests._support.smk import smk

pytestmark = [pytest.mark.asyncio]

_QUESTION = "I was charged twice for my subscription last month"
_HANDOFF_PROMPT = "Customer reports a duplicate subscription charge."


def _messages(root: Path, wid: str, sid: str) -> list[dict]:
    """The session's on-disk transcript. turn_log carries no text."""
    path = root / wid / ".state" / "sessions" / sid / "messages.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@smk("SMK-COOKBOOK-13")
async def test_front_line_hands_off_to_the_specialist(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    """The front-line agent hands the conversation to a billing specialist.

    The specialist is a real agent the switch has to resolve, so a handoff
    to a target that does not exist fails in the tool rather than at the
    checkpoint, long after the agent could react.
    """
    registry, base_url = mock_llm
    sfx = unique_suffix
    cleanup: list[str] = []
    try:
        # The specialist exists before the handoff names it.
        spec = await make_scripted_agent(
            authed_client, registry, base_url,
            suffix=f"spec{sfx}", scenario=f"scripted:desk-spec-{sfx}",
            rules=[Rule(emit_text="I have refunded the duplicate charge.")],
            system_prompt=["You are a billing specialist."],
        )
        cleanup.append(f"/v1/agents/{spec['agent_id']}")

        front = await make_scripted_agent(
            authed_client, registry, base_url,
            suffix=f"fl{sfx}", scenario=f"scripted:desk-fl-{sfx}",
            rules=[
                # Billing is not the front line's job: hand it over.
                Rule(
                    when_last_user_contains="charged twice",
                    emit_tool="system__switch_to_agent",
                    emit_args={
                        "agent_id": spec["agent_id"],
                        "prompt": _HANDOFF_PROMPT,
                    },
                ),
                Rule(when_tool_result=True, emit_text="Handing you over."),
            ],
            tools=["system__switch_to_agent"],
            system_prompt=["You are a front-line support agent."],
        )
        cleanup.append(f"/v1/agents/{front['agent_id']}")

        wid = await make_local_workspace(
            authed_client, suffix=f"desk{sfx}", root=tmp_path,
        )
        cleanup.append(f"/v1/workspaces/{wid}")

        sid = await start_agent_session(
            authed_client, workspace_id=wid,
            agent_id=front["agent_id"], instructions=_QUESTION,
        )
        await wait_completed(authed_client, sid)

        row = (await authed_client.get(f"/v1/sessions/{sid}")).json()

        # 1. The switch APPLIED, and it applied at the turn boundary: the
        #    queue is drained, not left pending.
        assert row["binding"]["agent_id"] == spec["agent_id"], (
            "the session should be bound to the specialist after the handoff"
        )
        assert not row.get("pending_binding_switch"), (
            "the queued switch should have been applied at the checkpoint"
        )

        # 2. Same session, and the switch is recorded rather than silent.
        assert row["id"] == sid
        assert row["binding_epoch"] >= 1, (
            "a handoff must bump the epoch so in-flight work is fenced"
        )

        records = _messages(tmp_path, wid, sid)
        markers = [r for r in records if r.get("kind") == "agent_marker"]
        assert markers, "the handoff left no agent marker in the transcript"
        payload = markers[-1]["payload"]
        assert payload["to_binding"]["agent_id"] == spec["agent_id"]
        assert payload["from_binding"]["agent_id"] == front["agent_id"]
        assert payload["actor"] == "agent", (
            "the AGENT decided this handoff, not an operator"
        )

        # 3. The customer's question is still in the history the specialist
        #    inherits: a handoff continues a conversation, it does not
        #    start one.
        text = json.dumps(records)
        assert _QUESTION in text, (
            "the transcript lost the customer's question across the handoff"
        )
    finally:
        for url in reversed(cleanup):
            await authed_client.delete(url)


@smk("SMK-COOKBOOK-13")
async def test_handoff_to_an_unknown_specialist_is_refused(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    """A handoff names its target by id, so a typo must surface to the
    agent as a tool error it can react to, not as a session bound to
    nothing."""
    registry, base_url = mock_llm
    sfx = unique_suffix
    cleanup: list[str] = []
    try:
        front = await make_scripted_agent(
            authed_client, registry, base_url,
            suffix=f"bad{sfx}", scenario=f"scripted:desk-bad-{sfx}",
            rules=[
                Rule(
                    when_last_user_contains="charged twice",
                    emit_tool="system__switch_to_agent",
                    emit_args={
                        "agent_id": f"no-such-agent-{sfx}",
                        "prompt": _HANDOFF_PROMPT,
                    },
                ),
                Rule(when_tool_result=True, emit_text="I could not hand off."),
            ],
            tools=["system__switch_to_agent"],
            system_prompt=["You are a front-line support agent."],
        )
        cleanup.append(f"/v1/agents/{front['agent_id']}")

        wid = await make_local_workspace(
            authed_client, suffix=f"deskbad{sfx}", root=tmp_path,
        )
        cleanup.append(f"/v1/workspaces/{wid}")

        sid = await start_agent_session(
            authed_client, workspace_id=wid,
            agent_id=front["agent_id"], instructions=_QUESTION,
        )
        await wait_completed(authed_client, sid)

        row = (await authed_client.get(f"/v1/sessions/{sid}")).json()
        assert row["binding"]["agent_id"] == front["agent_id"], (
            "a refused handoff must leave the binding where it was"
        )
        assert not row.get("pending_binding_switch")

        text = json.dumps(_messages(tmp_path, wid, sid))
        assert "does not exist" in text, (
            "the agent was not told why the handoff failed"
        )
    finally:
        for url in reversed(cleanup):
            await authed_client.delete(url)
