"""AskUserPanel cadence + cancel-modal-dismiss UI tests.

Covers backlog items:
* U0057 — "waiting since" affordance renders with parked_at timestamp.
* U0064 — Panel polls /ask_user/pending roughly every 2s while non-terminal.
* U0065 — Panel stops polling after the session reaches terminal status.
* U0069 — Cancel confirmation modal closes via ESC without sending the signal.

Uses Playwright page.route to mock /ask_user/pending (pattern from
commit 5a9b849) where needed; real REST API for session lifecycle.
"""

from __future__ import annotations

import json
import time

import httpx
from playwright.sync_api import expect

from tests.ui_e2e._studio_helpers import open_session_in_studio


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_llm_provider(base_url: str, pid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": pid, "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201


def _seed_agent(base_url: str, agent_id: str, provider_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/agents", json={
            "id": agent_id, "description": "cadence probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [], "system_prompt": ["test"],
        })
        assert r.status_code == 201


def _seed_workspace(base_url: str, wp_id: str, tpl_id: str, tmp_path) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id, "description": "ws tpl",
            "provider_id": wp_id, "backend": {"kind": "local"},
        })
        assert r.status_code == 201
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201
        return r.json()["id"]


def _seed_session(base_url: str, workspace_id: str, agent_id: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": agent_id},
                "auto_start": False,
            },
        )
        assert r.status_code == 201
        return r.json()["id"]


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


def _seed_ladder(base_url: str, unique_suffix: str, tmp_path):
    pid = f"llm-cd-{unique_suffix}"
    aid = f"ag-cd-{unique_suffix}"
    wp_id = f"wp-cd-{unique_suffix}"
    tpl_id = f"tpl-cd-{unique_suffix}"
    _seed_llm_provider(base_url, pid)
    _seed_agent(base_url, aid, pid)
    wid = _seed_workspace(base_url, wp_id, tpl_id, tmp_path)
    sid = _seed_session(base_url, wid, aid)
    cleanup_urls = [
        f"/v1/workspaces/{wid}/sessions/{sid}/cancel",
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    return wid, sid, cleanup_urls


# ===========================================================================
# U0057 — "waiting since" affordance renders parked_at timestamp
# ===========================================================================


# U0057 REMOVED (no Studio equivalent) — the retired session-detail
# AskUserPanel rendered a "waiting since {parked_at}" affordance in its
# header. The Studio's Action Required item (studio-activity.jsx
# ``action-item``) surfaces the yield's kind + prompt + inline controls but
# deliberately does NOT render a parked_at "waiting since" timestamp, so
# there is no affordance to pin. Removed with this note rather than
# asserting a surface the Studio does not render.


# ===========================================================================
# U0064 — Panel polls /ask_user/pending ~every 2s while non-terminal
# ===========================================================================


def test_u0064_panel_polls_pending_endpoint_while_non_terminal(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0064 — Re-pointed to the Studio session panel's polling. The old
    2s /ask_user/pending poll maps to the Studio's ``ST_SessionPanel``
    resource, which polls ``GET /v1/sessions/{sid}`` every 2s while the
    session is non-terminal (``pollMs: 2000`` + ``pauseWhile`` terminal,
    studio-center.jsx). Count the GETs while a CREATED session's agent
    panel is open — over ~7s we should see several polls.

    We observe (not fulfill) so the real 200 flows through and the panel
    keeps polling; a 404/blocked response would flip the panel to an
    error state and stop the cadence.
    """
    wid, sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        hits = {"count": 0}

        def _on_session_get(route):
            if route.request.method == "GET":
                hits["count"] += 1
            route.continue_()

        # Match the exact session-detail resource URL (not the nested
        # workspace endpoints). ST_SessionPanel fetches /v1/sessions/{sid}.
        page.route(f"**/v1/sessions/{sid}", _on_session_get)

        open_session_in_studio(page, console_url, wid, sid, kind="agent")

        # Observe ~7s of polling (2s cadence while CREATED).
        page.wait_for_timeout(7_500)
        assert hits["count"] >= 3, (
            f"expected >=3 /v1/sessions/{sid} polls in ~7s, got {hits['count']}"
        )
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0065 — Panel stops polling after session reaches terminal status
# ===========================================================================


def test_u0065_panel_stops_polling_after_terminal_status(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0065 — Once the session row is terminal (ended/cancelled/etc),
    the panel's useResource gate ``pollMs: isTerminal ? 0 : 2000``
    halts polling. Snapshot the counter, wait ~6s, assert it didn't
    advance (or at most advanced by 1 from a final in-flight call).
    """
    wid, sid, cleanup_urls = _seed_ladder(
        base_url, unique_suffix, tmp_path,
    )
    try:
        # Cancel the session via API → status=ENDED.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.post(f"/v1/workspaces/{wid}/sessions/{sid}/cancel")
            assert r.status_code == 200
            assert r.json()["status"] == "ended"

        hits = {"count": 0}

        def _on_pending(route):
            hits["count"] += 1
            route.fulfill(
                status=404, content_type="application/json",
                body=json.dumps({
                    "type": "/errors/not-found",
                    "title": "Not Found", "status": 404, "detail": "x",
                }),
            )

        page.route(
            f"**/v1/sessions/{sid}/ask_user/pending", _on_pending,
        )

        page.goto(
            f"{console_url}#/sessions/{sid}", wait_until="domcontentloaded",
        )
        # Resilience gate (chrome mounted).
        page.locator(".nav-item").first.wait_for(
            state="visible", timeout=20_000,
        )
        # Wait for status to be visibly terminal. The status pill is
        # somewhere in the body — just check the body text contains
        # "ended" or "cancelled".
        deadline = time.monotonic() + 10.0
        terminal_seen = False
        while time.monotonic() < deadline:
            body_text = (page.locator("body").text_content() or "").lower()
            if any(w in body_text for w in (
                "ended", "cancelled", "completed", "failed",
            )):
                terminal_seen = True
                break
            page.wait_for_timeout(300)
        assert terminal_seen, "page never reflected terminal status"

        # Snapshot the call counter, wait ~6s, assert ≤ snapshot + 1.
        # The +1 absorbs a final in-flight poll that started before the
        # isTerminal flag flipped.
        snapshot = hits["count"]
        page.wait_for_timeout(6_000)
        delta = hits["count"] - snapshot
        assert delta <= 1, (
            f"panel kept polling on terminal session: snapshot={snapshot}, "
            f"final={hits['count']}, delta={delta}"
        )
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0069 — Cancel confirmation modal ESC-dismisses without signaling
# ===========================================================================


# U0069 REMOVED (no Studio equivalent) — the retired session-detail Cancel
# button opened a "Cancel session?" confirmation modal, and this test pinned
# the ESC-dismiss-without-signal safety contract. The Studio's session
# controls (studio-center.jsx ``ST_SessionControls`` → ``ctrl-cancel``) fire
# the cancel POST DIRECTLY with no confirmation modal, so there is no
# modal-dismiss surface to pin. Removed with this note (the direct-cancel
# happy path is exercised by the re-pointed U0030 / U0103 journeys).
