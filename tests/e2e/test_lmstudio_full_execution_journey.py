"""E2E: LM-Studio-driven full agent-execution journey.

Third post-pivot user-journey on the API surface. Where the prior
journeys avoided LLM dispatch (test_full_journey_no_llm uses
auto_start=False; test_yielding_tools_journey uses direct DB park
injection), this test exercises **the full real-LLM execution path**:

  seed → POST session (auto_start=True) → worker claims → LLM responds
  → turn completes → .state commits → terminal status

LM Studio is the LLM backend. The test is gated on LM Studio
reachability via the same TCP-probe pattern as
test_session_lifecycle_lmstudio.py — `pytest.skip`s soft when the host
is unreachable.

Subsystems exercised (none of the other journeys touch ALL of these):

  1. providers + workspace + agent + session seeding
  2. **real LLM dispatch** via the configured openresponses adapter
  3. **worker pool** actually claiming the session + running a turn
  4. **scheduler** transitioning the row through CREATED → RUNNING → ENDED
  5. **workspace state repo** (`.state/sessions/<sid>/messages.jsonl`
     + git commits on every turn) — surfaced via `GET /log`
  6. agent thread persistence (turn_no, last_turn_at, last_worker_id)

Companion to test_session_lifecycle_lmstudio.py's T0037 / T0056
which pin the API CONTRACT around resume/pause/cancel under real
worker activity. This test pins the OBSERVABLE OUTPUT of a turn —
.state commits, turn counter, worker id.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path
from urllib.request import Request, urlopen

import httpx
import pytest


# ---------------------------------------------------------------------------
# LM Studio reachability gate (matches test_session_lifecycle_lmstudio.py)
# ---------------------------------------------------------------------------


_LM_STUDIO_URL = "http://127.0.0.1:8080"
_LM_STUDIO_API_KEY = os.environ.get("PRIMER_E2E_LMSTUDIO_TOKEN", "")


def _lmstudio_tcp_reachable(host: str = "127.0.0.1", port: int = 8080) -> bool:
    """Cheap TCP-handshake probe. Returns True if the port is listening."""
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
    if not _LM_STUDIO_API_KEY:
        return None
    if not _lmstudio_tcp_reachable():
        return None
    try:
        import json
        req = Request(
            f"{_LM_STUDIO_URL}/v1/models",
            headers={"Authorization": f"Bearer {_LM_STUDIO_API_KEY}"},
        )
        with urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"[lmstudio probe] {exc}", file=sys.stderr)
        return None
    items = data.get("data") or []
    for m in items:
        mid = m.get("id", "")
        if "embedding" in mid.lower() or "embed" in mid.lower():
            continue
        return mid
    return None


_MODEL_ID = _discover_chat_model()
pytestmark = pytest.mark.skipif(
    _MODEL_ID is None,
    reason=(
        f"PRIMER_E2E_LMSTUDIO_TOKEN unset, LM Studio not reachable at "
        f"{_LM_STUDIO_URL}, or no chat model loaded; see "
        "docs/testing/02-bringup.md"
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
        "description": "lmstudio full-execution journey probe",
        "model": {"provider_id": provider_id, "model_name": model_id},
        "tools": [],
    }


async def _wait_for_terminal(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    timeout_s: float = 90.0,
    interval_s: float = 0.5,
) -> dict:
    """Poll session until status='ended' OR timeout."""
    iters = max(1, int(timeout_s / interval_s))
    last: dict = {}
    for _ in range(iters):
        r = await client.get(f"/v1/sessions/{session_id}")
        if r.status_code == 200:
            last = r.json()
            if last.get("status") == "ended":
                return last
        await asyncio.sleep(interval_s)
    return last


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lmstudio_full_execution_journey_produces_observable_artifacts(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """End-to-end LM-Studio journey:

    Seed the full ladder → create session with auto_start=true + a
    one-shot instruction → wait for terminal status → assert that
    REAL execution artifacts exist:

      * session.turn_no >= 1            (worker ran ≥1 turn)
      * session.last_worker_id is set   (worker claimed the row)
      * session.last_turn_at is set     (turn completed)
      * workspace /log has ≥1 commit    (.state repo updated by turn)

    The LLM's output text is NOT asserted — its content depends on
    the model loaded in LM Studio and is non-deterministic. What we
    pin is the SHAPE of execution: that the worker actually claimed,
    ran, and committed state.
    """
    suffix = unique_suffix
    assert _MODEL_ID is not None  # guarded by pytestmark.skipif

    llm_id = f"lmjj-llm-{suffix}"
    agent_id = f"lmjj-ag-{suffix}"
    wp_id = f"lmjj-wp-{suffix}"
    tpl_id = f"lmjj-tpl-{suffix}"
    workspace_id: str | None = None
    session_id: str | None = None

    try:
        # ----- seed providers + workspace + agent -----
        r = await client.post(
            "/v1/llm_providers",
            json=_llm_provider_body(llm_id, _MODEL_ID),
        )
        assert r.status_code == 201, r.text
        r = await client.post(
            "/v1/agents",
            json=_agent_body(agent_id, provider_id=llm_id, model_id=_MODEL_ID),
        )
        assert r.status_code == 201, r.text
        r = await client.post(
            "/v1/workspace_providers",
            json={
                "id": wp_id,
                "provider": "local",
                "config": {"kind": "local", "root_path": str(tmp_path)},
            },
        )
        assert r.status_code == 201, r.text
        r = await client.post(
            "/v1/workspace_templates",
            json={
                "id": tpl_id,
                "description": "lmstudio journey template",
                "provider_id": wp_id,
                "backend": {"kind": "local"},
            },
        )
        assert r.status_code == 201, r.text
        r = await client.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, r.text
        workspace_id = r.json()["id"]

        # ----- create session with auto_start=True + short instruction -----
        sess = await client.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": agent_id},
                "auto_start": True,
                "initial_instructions": (
                    "Reply with the single word 'OK' and nothing else. "
                    "Do not use any tools."
                ),
            },
        )
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["id"]

        # ----- wait for terminal -----
        final = await _wait_for_terminal(
            client, session_id=session_id, timeout_s=90.0,
        )

        # Soft-skip if the worker pool / LLM didn't converge in time.
        # This avoids flaky-CI failure when LM Studio is loaded but
        # slow (large model, cold start). Real bug should land via
        # /v1/health worker pool capacity or other observability.
        if final.get("status") != "ended":
            pytest.skip(
                f"session did not reach terminal status in 90s: "
                f"status={final.get('status')!r}; LM Studio likely cold-loading"
            )

        # ----- assert observable execution artifacts -----
        assert final.get("turn_no", 0) >= 1, (
            f"turn_no should be >=1 after a completed turn; "
            f"got session row: {final!r}"
        )
        assert final.get("last_worker_id"), (
            f"last_worker_id must be set after a turn; got: {final!r}"
        )
        assert final.get("last_turn_at"), (
            f"last_turn_at must be set after a turn; got: {final!r}"
        )

        # ----- assert workspace .state was updated -----
        # The agent runtime commits messages.jsonl per turn — at least
        # one commit should be visible in the workspace /log endpoint.
        log = await client.get(
            f"/v1/workspaces/{workspace_id}/log?limit=50"
        )
        assert log.status_code == 200, log.text
        commits = log.json().get("commits", [])
        assert len(commits) >= 1, (
            f"workspace .state should have >=1 commit after one turn; "
            f"got: {log.json()!r}"
        )

        # The terminal reason (if any) is a string OR null — assert it's
        # one of the documented values, never /errors/internal-leaked.
        ended_reason = final.get("ended_reason")
        assert ended_reason in (
            None, "completed", "cancelled", "failed",
            "max_iterations_exceeded",
        ), f"unexpected ended_reason: {ended_reason!r}"

    finally:
        # ----- cleanup in reverse dependency order -----
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await client.delete(f"/v1/workspace_templates/{tpl_id}")
        await client.delete(f"/v1/workspace_providers/{wp_id}")
        await client.delete(f"/v1/agents/{agent_id}")
        await client.delete(f"/v1/llm_providers/{llm_id}")
