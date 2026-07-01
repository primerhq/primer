"""UI e2e: session state legibility + history. Default-skipped (conftest
ignores test_*.py unless PRIMER_RUN_UI_E2E=1)."""

from __future__ import annotations

import httpx
from playwright.sync_api import expect

from tests.ui_e2e._studio_helpers import (
    open_session_in_studio,
    open_studio,
    session_row,
)


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
        r = c.post("/v1/workspace_providers", json={
            "id": wp, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code in (201, 409), r.text
        r = c.post("/v1/workspace_templates", json={
            "id": tpl, "description": "tpl", "provider_id": wp,
            "backend": {"kind": "local"},
        })
        assert r.status_code in (201, 409), r.text
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
    it in the Studio, and assert the terminal outcome is surfaced.

    Re-pointed: the retired session-detail rendered a ``session-outcome``
    panel. The Studio's reused ``SessionLiveStream`` (inside ``panel-agent``)
    surfaces a terminal session as a "Session ended" notice, and the
    ``panel-agent-header`` StatusPill reads the terminal status — that is
    the closest real Studio surface for the "session's terminal outcome is
    legible" intent.
    """
    _seed_llm_provider(base_url, "sl-prov")
    _seed_agent(base_url, "sl-agent", "sl-prov")
    wid = _seed_workspace(base_url, "sl-wp", "sl-tpl", tmp_path)
    sid = _seed_agent_session(base_url, wid, "sl-agent")
    _cancel_session(base_url, wid, sid)

    open_session_in_studio(page, console_url, wid, sid, kind="agent")
    # The reused live-stream shows the terminal "Session ended" notice.
    expect(page.get_by_text("Session ended", exact=False).first).to_be_visible(
        timeout=20_000,
    )
    # And the panel header StatusPill reads a terminal status (ended).
    header = page.locator('[data-testid="panel-agent-header"]')
    expect(header.locator(".pill").filter(has_text="ended").first).to_be_visible(
        timeout=10_000,
    )


# ---------------------------------------------------------------------------
# Journey 2: cancelled session shows a "Cancelled" outcome chip in the list
# ---------------------------------------------------------------------------


def test_cancelled_session_shows_cancelled_chip_in_list(
    base_url, console_url, page, tmp_path,
) -> None:
    """Seed + cancel an agent session, then open the workspace's Studio
    and assert the cancelled session is listed in the sidebar with a
    terminal status dot.

    Re-pointed: the global ``#/sessions`` list (and its decoded "Cancelled"
    outcome chip) is retired. Sessions now live in the per-workspace Studio
    left-sidebar ``session-row`` list. That row surfaces terminal state via
    a status DOT (``session-status-dot``, gray tone for ended/cancelled)
    rather than a "Cancelled" text chip — there is no per-row outcome-label
    text in the Studio sidebar — so this pins the closest real surface: the
    cancelled row is present and carries its (terminal) status dot.
    """
    _seed_llm_provider(base_url, "sl-prov2")
    _seed_agent(base_url, "sl-agent2", "sl-prov2")
    wid = _seed_workspace(base_url, "sl-wp2", "sl-tpl2", tmp_path)
    sid = _seed_agent_session(base_url, wid, "sl-agent2")
    _cancel_session(base_url, wid, sid)

    open_studio(page, console_url, wid)
    # The cancelled session is listed as a sidebar row (located by its
    # data-session-id stamp — the row shows the title, not the raw sid).
    row = session_row(page, sid).first
    expect(row).to_be_visible(timeout=20_000)
    # The row carries its status dot (terminal / gray tone for cancelled).
    expect(row.locator('[data-testid="session-status-dot"]')).to_be_visible(
        timeout=10_000,
    )
