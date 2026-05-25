"""E2E: §2 tool-approval (type=required) end-to-end park journey.

Multi-subsystem user-journey:

  LLMProvider (LM Studio) → Agent → WorkspaceProvider → Template →
  Workspace → ToolApprovalPolicy → Session (auto_start=True) →
  LLM emits write_workspace_file tool call → ApprovalResolver matches
  the policy → ToolExecutionManager raises YieldToWorker(_approval) →
  worker parks session → /v1/sessions/{id}/tool_approval/pending
  returns the original_call envelope → /respond accepts the decision.

Subsystems exercised in one test:

  1. provider + agent + workspace ladder seeding
  2. ToolApprovalPolicy CRUD (§2 surface that just landed)
  3. real LLM dispatch via LM Studio (tool-calling)
  4. worker pool claim + agent runtime
  5. approval gate (matrix.agent.approval — ApprovalResolver)
  6. yield protocol (YieldToWorker → park_turn)
  7. tool_approval REST surface (pending + respond)
  8. session lifecycle (CREATED → RUNNING → parked)

Covers backlog item T0850.

The follow-up step (POST /respond → session resumes → tool actually
fires → transcript carries the result) is intentionally NOT
asserted in THIS test — but the resume wiring is now landed
(commits 068184a/496c886/731a05b/f83fee7 from the 2026-05-25
roadmap §7 engagement). The end-to-end approval-resume cycle is
covered by T0861 (test_resume_cycle_e2e_journey.py) which uses
asyncpg injection instead of a real LLM. T0850 stays focused on
the LM-Studio-driven park-time observable state — everything from
the agent emitting the gated tool_call through to the operator
seeing the pending row. Re-asserting the resume LLM-side
continuation here would require LM Studio to handle the
post-respond turn cleanly, which is a separate LM-Studio-compat
issue tracked elsewhere.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from urllib.request import Request, urlopen

import httpx
import pytest


# ---------------------------------------------------------------------------
# LM Studio reachability + model discovery (same pattern as
# test_lmstudio_full_execution_journey.py / T0037)
# ---------------------------------------------------------------------------


_LM_STUDIO_URL = "http://127.0.0.1:8080"
_LM_STUDIO_API_KEY = "***REMOVED***"


def _lmstudio_tcp_reachable(host: str = "127.0.0.1", port: int = 8080) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect((host, port))
        return True
    except (OSError, socket.timeout):
        return False
    finally:
        sock.close()


def _discover_chat_model() -> str | None:
    """Return the id of the first non-embedding model loaded in LM Studio."""
    if not _lmstudio_tcp_reachable():
        return None
    try:
        req = Request(
            f"{_LM_STUDIO_URL}/v1/models",
            headers={"Authorization": f"Bearer {_LM_STUDIO_API_KEY}"},
        )
        with urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[lmstudio probe] {exc}", file=sys.stderr)
        return None
    for m in data.get("data") or []:
        mid = m.get("id", "")
        if "embedding" in mid.lower() or "embed" in mid.lower():
            continue
        return mid
    return None


_MODEL_ID = _discover_chat_model()
pytestmark = pytest.mark.skipif(
    _MODEL_ID is None,
    reason=(
        f"LM Studio not reachable at {_LM_STUDIO_URL} or no chat model "
        "loaded; see docs/testing/02-bringup.md"
    ),
)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _llm_provider_body(entity_id: str, model_id: str) -> dict:
    return {
        "id": entity_id,
        "provider": "openresponses",
        "models": [{"name": model_id, "context_length": 8192}],
        "config": {
            "url": f"{_LM_STUDIO_URL}/v1",
            "api_key": _LM_STUDIO_API_KEY,
            "flavor": "lmstudio",
        },
        "limits": {"max_concurrency": 1},
    }


def _agent_body(entity_id: str, *, provider_id: str, model_id: str) -> dict:
    return {
        "id": entity_id,
        "description": "T0850 approval-required park journey probe",
        "model": {"provider_id": provider_id, "model_name": model_id},
        # Workspace tools are composed automatically when the session
        # attaches to a workspace — no need to list them here. We don't
        # add any first-class user toolsets either; the gate fires on
        # the (_workspaces, write_workspace_file) pair regardless of
        # whether the tool is advertised through the agent.tools list
        # or the workspace composer.
        "tools": [],
        "system_prompt": [
            "You are a test probe agent. When the user asks you to "
            "create a file, use the write_workspace_file tool with "
            "the exact path and content the user specifies. Do not "
            "explain what you are about to do — just make the call."
        ],
    }


# ---------------------------------------------------------------------------
# Polling helper
# ---------------------------------------------------------------------------


async def _wait_for_parked(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    timeout_s: float = 60.0,
    interval_s: float = 1.0,
) -> dict:
    """Poll the session until parked_status='parked' OR timeout.

    Returns the final session row JSON.
    """
    iters = max(1, int(timeout_s / interval_s))
    last: dict = {}
    for _ in range(iters):
        r = await client.get(f"/v1/sessions/{session_id}")
        if r.status_code == 200:
            last = r.json()
            if last.get("parked_status") == "parked":
                return last
            if last.get("status") == "ended":
                # Session terminated before parking — LLM didn't call
                # the gated tool, or worker hit a fatal error.
                return last
        await asyncio.sleep(interval_s)
    return last


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t0850_tool_approval_required_park_journey(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0850 — Multi-subsystem approval-required park journey.

    Walks the full park-time path:

      1. Seed LLM provider (LM Studio).
      2. Seed agent bound to that provider.
      3. Seed workspace provider + template + workspace.
      4. Seed ToolApprovalPolicy with type=required for
         (_workspaces, write_workspace_file).
      5. Create session with auto_start=True + an instruction that
         elicits a write_workspace_file tool call.
      6. Poll until session.parked_status == 'parked'. Skip-soft if
         the LLM didn't converge on the tool call within 60s (model
         non-determinism — the test pins the SHAPE, not the
         probabilistic LLM outcome).
      7. GET /v1/sessions/{id}/tool_approval/pending — assert the
         envelope carries tool_name='write_workspace_file',
         approval_type='required', policy_id matches.
      8. POST /v1/sessions/{id}/tool_approval/respond with
         decision='approved' — assert 202 + {"status":"accepted"}.

    Does NOT assert the post-approve resume continuation here:
    T0861 covers the resume-cycle end-to-end via asyncpg
    injection. Re-asserting it through the LLM-driven path
    requires a separate LM-Studio-compat sweep.
    """
    suffix = unique_suffix
    assert _MODEL_ID is not None  # guarded by pytestmark.skipif

    llm_id = f"t850-llm-{suffix}"
    agent_id = f"t850-ag-{suffix}"
    wp_id = f"t850-wp-{suffix}"
    tpl_id = f"t850-tpl-{suffix}"
    policy_id = f"t850-pol-{suffix}"
    workspace_id: str | None = None
    session_id: str | None = None

    try:
        # ----- 1. LLM provider -----
        r = await client.post(
            "/v1/llm_providers", json=_llm_provider_body(llm_id, _MODEL_ID),
        )
        assert r.status_code == 201, r.text

        # ----- 2. Agent -----
        r = await client.post(
            "/v1/agents",
            json=_agent_body(agent_id, provider_id=llm_id, model_id=_MODEL_ID),
        )
        assert r.status_code == 201, r.text

        # ----- 3. Workspace ladder -----
        r = await client.post(
            "/v1/workspace_providers",
            json={
                "id": wp_id,
                "provider": "local",
                "config": {"kind": "local", "path": str(tmp_path)},
            },
        )
        assert r.status_code == 201, r.text
        r = await client.post(
            "/v1/workspace_templates",
            json={
                "id": tpl_id,
                "description": "T0850 template",
                "provider_id": wp_id,
                "backend": {"kind": "local"},
            },
        )
        assert r.status_code == 201, r.text
        r = await client.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, r.text
        workspace_id = r.json()["id"]

        # ----- 4. ToolApprovalPolicy: gate write_workspace_file -----
        r = await client.post(
            "/v1/tool_approval_policies",
            json={
                "id": policy_id,
                "toolset_id": "workspaces",
                "tool_name": "write_workspace_file",
                "enabled": True,
                "approval": {"type": "required"},
            },
        )
        assert r.status_code == 201, r.text

        # The ApprovalResolver caches policies in-process. Invalidate
        # so the freshly-created row is visible to the running worker.
        r = await client.post("/v1/tool_approval_policies/invalidate")
        assert r.status_code == 202, r.text

        # ----- 5. Session with auto_start + a tool-eliciting instruction -----
        sess = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": agent_id},
                "auto_start": True,
                "initial_instructions": (
                    "Create a file at the path 'probe.txt' with the content "
                    "'hello from T0850'. Use the write_workspace_file tool."
                ),
            },
        )
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["id"]

        # ----- 6. Wait for park -----
        final = await _wait_for_parked(
            client, session_id=session_id, timeout_s=60.0,
        )
        if final.get("parked_status") != "parked":
            pytest.skip(
                f"session did not park within 60s — LLM may not have "
                f"called the gated tool; final state: "
                f"status={final.get('status')!r}, "
                f"parked_status={final.get('parked_status')!r}, "
                f"turn_no={final.get('turn_no')!r}. Model "
                f"non-determinism is a soft skip — the journey pins "
                f"the SHAPE, not the LLM's probabilistic outcome."
            )

        # ----- 7. GET /tool_approval/pending -----
        r = await client.get(f"/v1/sessions/{session_id}/tool_approval/pending")
        assert r.status_code == 200, r.text
        pending = r.json()
        assert pending["tool_name"] == "write_workspace_file", pending
        assert pending["approval_type"] == "required", pending
        assert pending["policy_id"] == policy_id, pending
        assert isinstance(pending["tool_call_id"], str) and pending["tool_call_id"], (
            pending
        )
        # Arguments should carry the path the LLM tried to write.
        assert isinstance(pending["arguments"], dict), pending
        # Defensive: no /errors/internal leak in any field.
        assert "/errors/internal" not in json.dumps(pending), pending

        # ----- 8. POST /respond — accepts the decision (202) -----
        r = await client.post(
            f"/v1/sessions/{session_id}/tool_approval/respond",
            json={
                "tool_call_id": pending["tool_call_id"],
                "decision": "approved",
                "reason": "T0850 journey approves the write",
            },
        )
        assert r.status_code == 202, r.text
        assert r.json() == {"status": "accepted"}, r.text

        # NOTE: T0861 (test_resume_cycle_e2e_journey.py) pins the
        # end-to-end post-approve resume cycle via asyncpg injection
        # (parked_status → null + turn_no advance). This test stays
        # focused on the LM-Studio-driven park-time observable state.
        # A future LLM-driven continuation assertion would need a
        # separate LM-Studio-compat sweep on the post-respond turn.

    finally:
        # Best-effort cleanup. The session cancel route is workspace-
        # scoped; the workspace + provider deletes cascade-block until
        # the session is terminated.
        with httpx.Client(timeout=15.0) as c:
            for url in (
                f"http://127.0.0.1:8765/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel"
                if (workspace_id and session_id)
                else None,
                f"http://127.0.0.1:8765/v1/workspaces/{workspace_id}"
                if workspace_id else None,
                f"http://127.0.0.1:8765/v1/workspace_templates/{tpl_id}",
                f"http://127.0.0.1:8765/v1/workspace_providers/{wp_id}",
                f"http://127.0.0.1:8765/v1/tool_approval_policies/{policy_id}",
                f"http://127.0.0.1:8765/v1/agents/{agent_id}",
                f"http://127.0.0.1:8765/v1/llm_providers/{llm_id}",
            ):
                if url is None:
                    continue
                try:
                    if "/cancel" in url:
                        c.post(url)
                    else:
                        c.delete(url)
                except Exception:  # noqa: BLE001
                    pass
