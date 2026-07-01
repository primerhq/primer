"""UI E2E: multi-page session-lifecycle user journey + workspace Sessions-tab polling.

Two complex multi-page journeys that traverse 3-4 console pages each,
mimicking a realistic operator workflow:

* U0103 — Sessions full-lifecycle journey: seed agent + workspace + session
  via API; user lands on /sessions, sees the row, drills into detail,
  clicks Cancel, confirms in the modal, observes the "Cancel signal
  sent" toast, watches the status caption poll to a terminal value,
  navigates back to the list via the breadcrumb, sees the row with a
  non-CREATED status pill.

* U0104 — Workspace detail Sessions tab reflects API-seeded session within
  the polling cadence: user lands on /workspaces/{wid}, clicks the
  Sessions tab (empty), a session is seeded via the API in the
  background, the tab's poll surfaces the row within ~10 s, the user
  clicks the row and lands on /sessions/{id}.

These belong to the UI loop's post-pivot focus on workspace+session
full-lifecycle flows (see ## ⚠️ PIVOT QUEUED block in the backlog).
Neither requires LM Studio — the session stays in CREATED until the
operator cancels (or the worker tries to claim it and fast-fails on
the placeholder LLM URL, which is fine — both outcomes are
observable in the UI within the polling cadence).
"""

from __future__ import annotations

import time

import httpx
from playwright.sync_api import expect

from tests.ui_e2e._studio_helpers import open_studio


# ---------------------------------------------------------------------------
# Container-internal workspace provider path — host tmp_path is not visible
# from the primer-app container; using /tmp/<suffix> inside the container
# avoids the U0072/U0080-class host-path unreachability problem.
# ---------------------------------------------------------------------------


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-06")


def _container_ws_root(suffix: str) -> str:
    return f"/tmp/u0103-{suffix}"


def _seed_session_ladder(
    base_url: str, suffix: str,
) -> dict[str, str]:
    """Seed LLM provider → workspace provider → template → workspace →
    agent → session via the API. Returns the ids.

    Session is created with auto_start=False so it stays in CREATED
    indefinitely (the worker pool only claims auto-started sessions in
    this iteration's bring-up).
    """
    ids = {
        "llm": f"j-llm-{suffix}",
        "wp": f"j-wp-{suffix}",
        "tpl": f"j-tpl-{suffix}",
        "agent": f"j-ag-{suffix}",
        "workspace": "",  # backend-assigned
        "session": "",     # backend-assigned
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": ids["llm"],
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed llm failed: {r.text}"
        r = c.post("/v1/workspace_providers", json={
            "id": ids["wp"],
            "provider": "local",
            "config": {"kind": "local", "root_path": _container_ws_root(suffix)},
        })
        assert r.status_code == 201, f"seed wp failed: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": ids["tpl"],
            "description": "journey template",
            "provider_id": ids["wp"],
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed tpl failed: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, f"seed workspace failed: {r.text}"
        ids["workspace"] = r.json()["id"]
        r = c.post("/v1/agents", json={
            "id": ids["agent"],
            "description": "journey agent",
            "model": {"provider_id": ids["llm"], "model_name": "fake-model"},
            "tools": [],
            "system_prompt": ["probe"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"
        r = c.post(
            f"/v1/workspaces/{ids['workspace']}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": ids["agent"]},
                "auto_start": False,
            },
        )
        assert r.status_code == 201, f"seed session failed: {r.text}"
        ids["session"] = r.json()["id"]
    return ids


def _cleanup(base_url: str, ids: dict[str, str]) -> None:
    """Best-effort unwind; ignore individual failures so one stale row
    doesn't mask the rest."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in (
            f"/v1/workspaces/{ids['workspace']}/sessions/{ids['session']}/cancel"
            if ids.get("session") else None,
            f"/v1/workspaces/{ids['workspace']}" if ids.get("workspace") else None,
            f"/v1/workspace_templates/{ids['tpl']}",
            f"/v1/workspace_providers/{ids['wp']}",
            f"/v1/agents/{ids['agent']}",
            f"/v1/llm_providers/{ids['llm']}",
        ):
            if url is None:
                continue
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# U0103 — Sessions full-lifecycle journey: list→detail→Cancel→ENDED→list
# ===========================================================================


def test_u0103_sessions_full_lifecycle_journey(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    console_messages: list[dict],
    failed_requests: list[dict],
) -> None:
    """U0103 — Multi-page session-lifecycle journey.

    Steps:
      1. Seed agent + workspace + session via API.
      2. Navigate to /sessions list — assert seeded session row visible.
      3. Click the row → land on /sessions/{id}.
      4. Click the Cancel button → confirmation modal opens.
      5. Click the "Cancel session" confirm button.
      6. Assert "Cancel signal sent" toast appears.
      7. Watch the status pill poll OFF of CREATED within the
         polling cadence (the API mutation flips the row to
         cancelled/ending; UI polls /v1/sessions/{id} every 2 s).
      8. Click the "Sessions" breadcrumb to navigate back to /sessions.
      9. Assert the row is still listed with a non-CREATED status.

    Pages traversed: /console/ → /sessions → /sessions/{id} → /sessions.
    Signals fired: cancel via UI.
    Observed: list-row visibility, detail polling, terminal status pill,
    breadcrumb navigation.
    """
    ids = _seed_session_ladder(base_url, unique_suffix)
    wid = ids["workspace"]
    sid = ids["session"]
    try:
        # --- 1. Enter the Studio; the seeded session is a sidebar row -------
        open_studio(page, console_url, wid)
        row_locator = page.locator('[data-testid="session-row"]', has_text=sid)
        expect(row_locator.first).to_be_visible(timeout=20_000)

        # --- 2. Click the row → center tab + agent panel --------------------
        row_locator.first.click()
        expect(page.locator('[data-testid="center-tab"]').first).to_be_visible(
            timeout=15_000,
        )
        expect(page.locator('[data-testid="panel-agent"]')).to_be_visible(
            timeout=15_000,
        )

        # Sanity: the ctrl-cancel control is enabled (session non-terminal).
        cancel_btn = page.locator('[data-testid="ctrl-cancel"]').first
        expect(cancel_btn).to_be_enabled(timeout=10_000)

        # --- 3. Click ctrl-cancel (fires directly, no confirm modal) --------
        cancel_btn.click()
        # Toast from ST_SessionControls has title "Cancel signal sent".
        expect(page.get_by_text("Cancel signal sent")).to_be_visible(
            timeout=10_000,
        )

        # --- 4. Panel-header status polls off CREATED -----------------------
        # ST_SessionPanel polls /sessions/{id} every 2s while non-terminal;
        # the header StatusPill catches up to cancelled/ended. Pin "left
        # CREATED" (30s budget).
        header = page.locator('[data-testid="panel-agent-header"]')
        status_pill = header.locator(".pill").first
        deadline = time.time() + 30.0
        last_seen = None
        while time.time() < deadline:
            txt = status_pill.text_content(timeout=2_000) or ""
            last_seen = txt.strip().lower()
            if last_seen and "created" not in last_seen:
                break
            page.wait_for_timeout(500)
        else:
            raise AssertionError(
                f"status pill never left CREATED within 30s; last seen: {last_seen!r}"
            )

        # --- 5. The session is still listed in the Studio sidebar -----------
        row_after = page.locator('[data-testid="session-row"]', has_text=sid)
        expect(row_after.first).to_be_visible(timeout=15_000)
    finally:
        _cleanup(base_url, ids)


# ===========================================================================
# U0104 — Workspace detail Sessions tab reflects API-seeded session
# ===========================================================================


def test_u0104_workspace_sessions_tab_reflects_api_seeded_session(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0104 — Workspace detail Sessions tab polling cadence.

    Steps:
      1. Seed agent + workspace (no session yet) via API.
      2. Navigate to /workspaces/{wid}.
      3. Click the Sessions tab.
      4. Assert the empty-state "No sessions" copy is visible.
      5. Seed a session bound to the workspace via API.
      6. Wait for SessionsTab's 5 s poll to surface the new row
         (≤15 s budget).
      7. Click the session row → land on /sessions/{id}.

    Pages traversed: /console/ → /workspaces/{wid}?tab=sessions →
    /sessions/{id}.
    The tab is the SessionsTab in workspaces.jsx:729 — useResource with
    pollMs=5000 means a fresh row should land within ~10 s of being POSTed.
    """
    ids: dict[str, str] = {
        "llm": f"j-llm-104-{unique_suffix}",
        "wp": f"j-wp-104-{unique_suffix}",
        "tpl": f"j-tpl-104-{unique_suffix}",
        "agent": f"j-ag-104-{unique_suffix}",
        "workspace": "",
        "session": "",
    }
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.post("/v1/llm_providers", json={
                "id": ids["llm"],
                "provider": "ollama",
                "config": {"url": "http://127.0.0.1:9999"},
                "models": [{"name": "fake-model", "context_length": 4096}],
                "limits": {"max_concurrency": 1},
            })
            assert r.status_code == 201, f"seed llm: {r.text}"
            r = c.post("/v1/workspace_providers", json={
                "id": ids["wp"],
                "provider": "local",
                "config": {"kind": "local", "root_path": f"/tmp/u0104-{unique_suffix}"},
            })
            assert r.status_code == 201, f"seed wp: {r.text}"
            r = c.post("/v1/workspace_templates", json={
                "id": ids["tpl"],
                "description": "u0104 tpl",
                "provider_id": ids["wp"],
                "backend": {"kind": "local"},
            })
            assert r.status_code == 201, f"seed tpl: {r.text}"
            r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
            assert r.status_code == 201, f"seed ws: {r.text}"
            ids["workspace"] = r.json()["id"]
            r = c.post("/v1/agents", json={
                "id": ids["agent"],
                "description": "u0104 agent",
                "model": {"provider_id": ids["llm"], "model_name": "fake-model"},
                "tools": [],
                "system_prompt": ["probe"],
            })
            assert r.status_code == 201, f"seed agent: {r.text}"

        wid = ids["workspace"]

        # --- 1. Enter the Studio for the workspace --------------------------
        # Re-pointed: the workspace-detail Sessions tab is retired; sessions
        # live in the Studio left-sidebar Sessions section, which polls
        # /workspaces/{wid}/sessions every 3s (studio-sidebar.jsx).
        open_studio(page, console_url, wid)

        # --- 2. Sessions section shows the empty state ----------------------
        # SessionsSection renders "No sessions yet." before any is seeded.
        expect(page.locator('[data-testid="sessions-section"]')).to_be_visible(
            timeout=15_000,
        )
        expect(
            page.locator('[data-testid="sessions-section"]').get_by_text(
                "No sessions yet", exact=False,
            )
        ).to_be_visible(timeout=15_000)

        # --- 3. Seed the session in the background --------------------------
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.post(
                f"/v1/workspaces/{wid}/sessions",
                json={
                    "binding": {"kind": "agent", "agent_id": ids["agent"]},
                    "auto_start": False,
                },
            )
            assert r.status_code == 201, f"seed session: {r.text}"
            ids["session"] = r.json()["id"]
        sid = ids["session"]

        # --- 4. Wait for the sidebar row to surface within the 3s poll ------
        row_locator = page.locator('[data-testid="session-row"]', has_text=sid)
        expect(row_locator.first).to_be_visible(timeout=20_000)

        # --- 5. Click the row → center tab + agent panel --------------------
        row_locator.first.click()
        expect(page.locator('[data-testid="center-tab"]').first).to_be_visible(
            timeout=15_000,
        )
        expect(page.locator('[data-testid="panel-agent"]')).to_be_visible(
            timeout=15_000,
        )
    finally:
        _cleanup(base_url, ids)
