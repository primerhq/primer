"""UI e2e: session state legibility + history. Default-skipped (conftest
ignores test_*.py unless PRIMER_RUN_UI_E2E=1)."""

from __future__ import annotations

import httpx
from playwright.sync_api import expect


# ---------------------------------------------------------------------------
# Seed helpers — mirrored from test_graph_run_view_journey.py, adapted for
# an agent-bound (not graph-bound) session.
# ---------------------------------------------------------------------------


def _seed_llm_provider(base_url: str, pid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": pid, "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code in (201, 409), r.text


def _seed_agent(base_url: str, aid: str, pid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/agents", json={
            "id": aid, "description": "state-legibility probe",
            "model": {"provider_id": pid, "model_name": "fake-model"},
            "tools": [], "system_prompt": ["test"],
        })
        assert r.status_code in (201, 409), r.text


def _seed_workspace(base_url: str, wp: str, tpl: str, tmp_path) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        c.post("/v1/workspace_providers", json={
            "id": wp, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        c.post("/v1/workspace_templates", json={
            "id": tpl, "description": "tpl", "provider_id": wp,
            "backend": {"kind": "local"},
        })
        r = c.post("/v1/workspaces", json={"template_id": tpl})
        assert r.status_code == 201, r.text
        return r.json()["id"]


def _seed_agent_session(base_url: str, wid: str, aid: str) -> str:
    """Create an agent-bound session (auto_start=False → stays in CREATED)."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(f"/v1/workspaces/{wid}/sessions", json={
            "binding": {"kind": "agent", "agent_id": aid},
            "auto_start": False,
        })
        assert r.status_code == 201, r.text
        return r.json()["id"]


def _cancel_session(base_url: str, wid: str, sid: str) -> None:
    """Hard-cancel a CREATED session → ENDED/cancelled (terminal state)."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(f"/v1/workspaces/{wid}/sessions/{sid}/cancel")
        assert r.status_code in (200, 409), r.text


# ---------------------------------------------------------------------------
# Journey 1: cancelled session shows outcome banner on detail page
# ---------------------------------------------------------------------------


def test_cancelled_session_shows_outcome_banner(
    base_url, console_url, page, tmp_path,
) -> None:
    """Seed an agent session, cancel it via API (→ ENDED/cancelled), open
    the detail page, and assert the outcome banner is visible."""
    _seed_llm_provider(base_url, "sl-prov")
    _seed_agent(base_url, "sl-agent", "sl-prov")
    wid = _seed_workspace(base_url, "sl-wp", "sl-tpl", tmp_path)
    sid = _seed_agent_session(base_url, wid, "sl-agent")
    _cancel_session(base_url, wid, sid)

    page.goto(f"{console_url}#/sessions/{sid}")
    expect(page.locator('[data-testid="session-outcome"]')).to_be_visible(
        timeout=20_000,
    )


# ---------------------------------------------------------------------------
# Journey 2: cancelled session appears under the "Failed" filter on the list
# ---------------------------------------------------------------------------


def test_cancelled_session_visible_under_failed_filter(
    base_url, console_url, page, tmp_path,
) -> None:
    """Seed + cancel an agent session, then open /sessions, click the
    "Failed" chip, and assert the session id appears in the filtered list."""
    _seed_llm_provider(base_url, "sl-prov2")
    _seed_agent(base_url, "sl-agent2", "sl-prov2")
    wid = _seed_workspace(base_url, "sl-wp2", "sl-tpl2", tmp_path)
    sid = _seed_agent_session(base_url, wid, "sl-agent2")
    _cancel_session(base_url, wid, sid)

    page.goto(f"{console_url}#/sessions")
    page.get_by_text("Failed", exact=True).first.click()
    expect(page.get_by_text(sid[:8], exact=False).first).to_be_visible(
        timeout=20_000,
    )
