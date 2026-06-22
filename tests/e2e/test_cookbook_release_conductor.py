"""Cookbook recipe #10 regression: Release Conductor (deploy gate / HITL).

Guards the SESSION-HITL loop a DevOps "Release Conductor" agent relies on:

1. ``system__ask_user`` PARK + RESUME -- on an ambiguous deploy request the
   agent asks the operator to confirm the target/version; the session parks,
   the operator answers over ``POST /v1/sessions/{id}/ask_user/respond``, and
   the turn resumes.
2. A REQUIRED tool-approval gate on the irreversible deploy tool
   (``ToolApprovalPolicy{approval:{type:"required"}}``). The agent then calls
   the deploy tool; the gate trips and the session parks again on
   ``_approval``.
3. BOTH verdicts over ``POST /v1/sessions/{id}/tool_approval/respond``:
   * APPROVED -> the gated deploy tool actually RUNS (re-dispatched with
     ``bypass_approval``); the session ends ``completed`` and the deploy
     side effect (a ``RELEASE`` marker file) is on disk.
   * REJECTED -> the gated call resolves to a rejection result, the agent
     aborts WITHOUT the deploy side effect, the session ends, and a durable
     ``ToolApprovalRecord{decision:"rejected"}`` is written (the denial audit
     trail).

Recipe: primerhq.github.io/docs_source/cookbook/release-conductor.md

The "irreversible deploy" tool is the built-in ``workspaces__write_workspace_file``
(it executes for real on approve and is re-dispatchable through the approval
gate). In the published recipe this stands in for a ``deploy-ops__run_deploy``
MCP tool -- the test pins the GATE MECHANISM, which is tool-agnostic. The
deploy is modelled as "write the RELEASE marker file", so the on-disk file is
the observable deploy side effect.

The HITL decision is operator-driven over the REST endpoints, NEVER scripted
into the mock LLM (the LLM only chooses to call ask_user then the deploy tool;
the operator chooses approve/reject). The transcript markers are asserted from
the on-disk ``<ws>/.state/sessions/<sid>/messages.jsonl`` (``turn_log`` carries
no text); the park/resume + records are asserted from the HITL REST surface.

Run with:
    PRIMER_RUN_E2E=1 uv run pytest tests/e2e/test_cookbook_release_conductor.py -n0 -q
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
    wait_terminal,
)
from tests._support.smk import smk

pytestmark = [pytest.mark.asyncio]


_DEPLOY_PATH = "RELEASE"
_DEPLOY_CONTENT = "staging v1.4.2"
# The gated deploy tool. The approval policy is keyed on the bare
# ``(toolset_id, tool_name)`` pair (``workspaces`` / ``write_workspace_file``),
# but the pending-approval echo and the durable ToolApprovalRecord both report
# the call's NAMESPACED name (``original_call.name``), so assert against that.
_DEPLOY_TOOL = "workspaces__write_workspace_file"
# The operator's ask_user answer; its presence in the resumed tool_result is
# what flips the scripted agent from "ask" to "deploy".
_OPERATOR_ANSWER = "staging, v1.4.2"


# ---------------------------------------------------------------------------
# Transcript helpers
# ---------------------------------------------------------------------------


def _iter_parts(transcript: str, role: str, part_type: str):
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("role") != role:
            continue
        for part in obj.get("parts", []):
            if part.get("type") == part_type:
                yield part


def _tool_calls(transcript: str) -> list[dict]:
    return list(_iter_parts(transcript, "assistant", "tool_call"))


def _tool_results(transcript: str) -> list[dict]:
    return list(_iter_parts(transcript, "tool", "tool_result"))


def _call_names(transcript: str) -> list[str]:
    return [(p.get("name") or p.get("tool_name") or "") for p in _tool_calls(transcript)]


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _make_required_policy(client: httpx.AsyncClient, *, policy_id: str) -> str:
    """Create a REQUIRED approval policy on the deploy tool.

    Policies are unique on ``(toolset_id, tool_name)``; clear any leftover for
    the pair first so re-runs don't 409.
    """
    existing = await client.get("/v1/tool_approval_policies")
    if existing.status_code == 200:
        for it in existing.json().get("items", []):
            if (
                it.get("toolset_id") == "workspaces"
                and it.get("tool_name") == "write_workspace_file"
            ):
                await client.delete(f"/v1/tool_approval_policies/{it['id']}")
    r = await client.post(
        "/v1/tool_approval_policies",
        json={
            "id": policy_id,
            "toolset_id": "workspaces",
            "tool_name": "write_workspace_file",
            "enabled": True,
            "approval": {"type": "required"},
            "timeout_seconds": 600,
        },
    )
    assert r.status_code in (200, 201), r.text
    # The ApprovalResolver caches policies in-process; invalidate so the
    # freshly-created row is visible to the running worker.
    r = await client.post("/v1/tool_approval_policies/invalidate")
    assert r.status_code == 202, r.text
    return policy_id


def _release_conductor_rules(
    *, workspace_id: str, abort_on_reject: bool,
) -> list[Rule]:
    """Deterministic rules: ask_user -> deploy -> report.

    Discriminated purely by the most recent tool-result content (the operator
    decision is NOT scripted here):
      * no tool result yet              -> ask_user (confirm env+version)
      * last result is the rejection    -> abort report (reject path)
      * last result is the ask_user reply (``{"response": ...}``)
                                        -> write_workspace_file (the deploy)
      * otherwise (deploy ran)          -> success report (approve path)

    Rule ORDER matters: the mock returns the FIRST matching rule. The rejection
    tool_result echoes the original deploy ``arguments`` (so it still contains
    ``"staging"``), so the rejection rule MUST be checked before the deploy
    rule, and the deploy rule keys off the ask_user reply envelope
    (``"response"``) -- which the rejection result does NOT carry -- not a bare
    ``"staging"`` substring. Without both, a rejected gate re-matches the deploy
    rule, re-deploys, re-trips the gate, and the session parks forever.

    ``workspace_id`` is baked into the deploy call's args: the
    ``workspaces__write_workspace_file`` internal tool requires it (the
    bound-session workspace is not auto-injected for the internal-toolset
    route), so the scripted call must carry the real id.
    """
    rules: list[Rule] = [
        Rule(
            when_tool_result=False,
            emit_tool="system__ask_user",
            emit_args={
                "prompt": "Which environment and version should I deploy?"
            },
        ),
    ]
    if abort_on_reject:
        rules.append(
            Rule(
                when_last_tool_result_contains="rejected",
                emit_text="Deploy aborted: the operator denied the release.",
            )
        )
    rules.append(
        Rule(
            # The ask_user resolution is ``{"response": "staging, v1.4.2"}``;
            # match on the envelope key so the rejection echo (which lacks it)
            # cannot re-trigger the deploy.
            when_last_tool_result_contains='"response"',
            emit_tool="workspaces__write_workspace_file",
            emit_args={
                "workspace_id": workspace_id,
                "path": _DEPLOY_PATH,
                "content": _DEPLOY_CONTENT,
            },
        )
    )
    rules.append(Rule(when_tool_result=True, emit_text="Deploy complete."))
    return rules


async def _wait_parked_on(
    client: httpx.AsyncClient,
    sid: str,
    *,
    tool_name: str,
    timeout_s: float = 30.0,
    interval_s: float = 0.25,
) -> dict:
    """Poll until the session parks on ``tool_name`` (the inner yielded tool).

    Returns the session row. Fails if the session ends before parking.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/sessions/{sid}")
        if r.status_code == 200:
            last = r.json()
            if last.get("parked_status") == "parked":
                yielded = (last.get("parked_state") or {}).get("yielded") or {}
                if yielded.get("tool_name") == tool_name:
                    return last
            if last.get("status") == "ended":
                raise AssertionError(
                    f"session {sid} ended before parking on {tool_name!r}: "
                    f"reason={last.get('ended_reason')!r}"
                )
        await asyncio.sleep(interval_s)
    raise AssertionError(
        f"session {sid} never parked on {tool_name!r} within {timeout_s}s; "
        f"last={last!r}"
    )


async def _drive_to_approval_park(
    client: httpx.AsyncClient,
    registry,
    base_url: str,
    *,
    suffix: str,
    tmp_path: Path,
    abort_on_reject: bool,
) -> tuple[str, str, str]:
    """Build the conductor, run it, answer the ask_user park, and stop on the
    approval park. Returns ``(workspace_id, session_id, approval_tool_call_id)``.
    """
    # The deploy call must carry the real workspace id, so the workspace is
    # created BEFORE the scripted rules are registered.
    wid = await make_local_workspace(client, suffix=suffix, root=tmp_path)
    agent = await make_scripted_agent(
        client, registry, base_url, suffix=suffix,
        scenario=f"scripted:{suffix}",
        tools=["system__ask_user", "workspaces__write_workspace_file"],
        system_prompt=[
            "You are a Release Conductor. Confirm the target and version with "
            "ask_user, then deploy by writing the RELEASE marker. Never deploy "
            "without confirming."
        ],
        rules=_release_conductor_rules(
            workspace_id=wid, abort_on_reject=abort_on_reject,
        ),
    )
    sid = await start_agent_session(
        client, workspace_id=wid, agent_id=agent["agent_id"],
        instructions="Deploy the latest build.",
    )

    # ---- 1. ask_user park ----
    await _wait_parked_on(client, sid, tool_name="ask_user")
    r = await client.get(f"/v1/sessions/{sid}/ask_user/pending")
    assert r.status_code == 200, r.text
    ask_pending = r.json()
    assert ask_pending["prompt"], ask_pending
    r = await client.post(
        f"/v1/sessions/{sid}/ask_user/respond",
        json={
            "tool_call_id": ask_pending["tool_call_id"],
            "response": _OPERATOR_ANSWER,
        },
    )
    assert r.status_code == 202, r.text

    # ---- 2. approval park (the gate tripped on the deploy tool) ----
    await _wait_parked_on(client, sid, tool_name="_approval")
    r = await client.get(f"/v1/sessions/{sid}/tool_approval/pending")
    assert r.status_code == 200, r.text
    approval = r.json()
    assert approval["tool_name"] == _DEPLOY_TOOL, approval
    assert approval["approval_type"] == "required", approval
    assert approval["arguments"].get("path") == _DEPLOY_PATH, approval
    return wid, sid, approval["tool_call_id"]


# ===========================================================================
# Test
# ===========================================================================


@smk("SMK-COOKBOOK-10")
async def test_release_conductor_ask_user_then_gated_deploy(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    """Full session-HITL loop: ask_user park/resume, then a REQUIRED approval
    gate on the deploy tool resolved BOTH ways (approve runs it, reject aborts
    + records the denial)."""
    registry, base_url = mock_llm

    # ----------------------------------------------------------------
    # Shared: one REQUIRED policy on the deploy tool for both runs.
    # ----------------------------------------------------------------
    policy_id = await _make_required_policy(
        authed_client, policy_id=f"rc-pol-{unique_suffix}",
    )

    # ================================================================
    # APPROVE path: the gated deploy actually runs.
    # ================================================================
    wid_a, sid_a, tcid_a = await _drive_to_approval_park(
        authed_client, registry, base_url,
        suffix=f"rc-ok-{unique_suffix}", tmp_path=tmp_path,
        abort_on_reject=False,
    )
    r = await authed_client.post(
        f"/v1/sessions/{sid_a}/tool_approval/respond",
        json={"tool_call_id": tcid_a, "decision": "approved"},
    )
    assert r.status_code == 202, r.text

    final_a = await wait_terminal(authed_client, sid_a, timeout_s=60)
    assert final_a.get("status") == "ended", final_a
    assert final_a.get("ended_reason") == "completed", final_a
    assert final_a.get("parked_status") is None, final_a

    msgs_a = tmp_path / wid_a / ".state" / "sessions" / sid_a / "messages.jsonl"
    assert msgs_a.exists(), f"no transcript at {msgs_a}"
    transcript_a = msgs_a.read_text(encoding="utf-8")
    names_a = _call_names(transcript_a)
    assert any("ask_user" in n for n in names_a), (
        f"approve path missing ask_user call: {names_a}"
    )
    assert any("write_workspace_file" in n for n in names_a), (
        f"approve path missing deploy call: {names_a}"
    )
    # The deploy actually ran: the RELEASE marker is on disk.
    release_file = tmp_path / wid_a / _DEPLOY_PATH
    assert release_file.exists(), (
        f"approved deploy did not write the RELEASE marker at {release_file}"
    )
    assert release_file.read_text(encoding="utf-8") == _DEPLOY_CONTENT, (
        release_file.read_text(encoding="utf-8")
    )
    # The DEPLOY tool_result specifically ran and succeeded: its output carries
    # the write_workspace_file shape ({path, size_bytes}). Matching on those
    # keys distinguishes it from the ask_user resolution ({response: ...}),
    # which would slip past a bare "not a rejection" filter.
    deploy_results_a = [
        out
        for out in (str(res.get("output") or "") for res in _tool_results(transcript_a))
        if "size_bytes" in out and _DEPLOY_PATH in out and '"rejected"' not in out
    ]
    assert deploy_results_a, (
        f"approve path recorded no successful deploy tool_result "
        f"({{path, size_bytes}}): {transcript_a!r}"
    )

    # ================================================================
    # REJECT path: the gated deploy is denied; no side effect.
    # ================================================================
    wid_r, sid_r, tcid_r = await _drive_to_approval_park(
        authed_client, registry, base_url,
        suffix=f"rc-no-{unique_suffix}", tmp_path=tmp_path,
        abort_on_reject=True,
    )
    r = await authed_client.post(
        f"/v1/sessions/{sid_r}/tool_approval/respond",
        json={
            "tool_call_id": tcid_r,
            "decision": "rejected",
            "reason": "change freeze window",
        },
    )
    assert r.status_code == 202, r.text

    final_r = await wait_terminal(authed_client, sid_r, timeout_s=60)
    assert final_r.get("status") == "ended", final_r
    assert final_r.get("parked_status") is None, final_r

    msgs_r = tmp_path / wid_r / ".state" / "sessions" / sid_r / "messages.jsonl"
    assert msgs_r.exists(), f"no transcript at {msgs_r}"
    transcript_r = msgs_r.read_text(encoding="utf-8")
    names_r = _call_names(transcript_r)
    assert any("write_workspace_file" in n for n in names_r), (
        f"reject path: the deploy was never offered: {names_r}"
    )
    # The gated call resolved to a rejection result, not a success.
    rejection_results = [
        str(res.get("output") or "")
        for res in _tool_results(transcript_r)
        if '"rejected"' in str(res.get("output") or "")
    ]
    assert rejection_results, (
        f"reject path: no rejection tool_result recorded: {transcript_r!r}"
    )
    assert "change freeze window" in rejection_results[0], rejection_results
    # No deploy side effect: the RELEASE marker was NOT written for this ws.
    rejected_release = tmp_path / wid_r / _DEPLOY_PATH
    assert not rejected_release.exists(), (
        f"rejected deploy still wrote the marker at {rejected_release}"
    )

    # ---- The durable denial audit trail: a ToolApprovalRecord(rejected). ----
    r = await authed_client.get(
        "/v1/tool_approval/records", params={"status": "rejected", "length": 50},
    )
    assert r.status_code == 200, r.text
    records = r.json().get("items", [])
    ours = [rec for rec in records if rec.get("session_id") == sid_r]
    assert ours, (
        f"no rejected ToolApprovalRecord written for session {sid_r}: "
        f"{records!r}"
    )
    rec = ours[0]
    assert rec["decision"] == "rejected", rec
    assert rec["tool_name"] == _DEPLOY_TOOL, rec
    assert rec.get("reason") == "change freeze window", rec
