"""UI E2E: multi-page operator troubleshooting journey.

Chases cross-page references the way an actual operator would
investigate a session — list → detail → drill into the agent →
back → drill into the workspace → Sessions tab → back to session
detail via row click. Seven page transitions, three cross-page
reference clicks, all bound to the same seeded session.

What makes this distinct from U0103 (sessions cancel lifecycle) and
test_full_operator_journey (page enumeration): this journey
exercises the **References panel** at session-detail.jsx:380-414
which is the operator's main launchpad for digging into adjacent
entities mid-investigation. Without this test, regressions in those
anchor handlers (agent / workspace / worker links) ship silently.

Covers backlog item U0105.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect

from tests.ui_e2e._studio_helpers import open_studio, session_row
from tests._support.model_profiles import agent_model, seed_llm_provider_with


# ---------------------------------------------------------------------------
# Container-internal workspace provider path — host tmp_path is not
# visible from the primer-app container; using /tmp/<suffix> inside the
# container avoids the U0072-class host-path unreachability problem.
# ---------------------------------------------------------------------------


def _container_ws_root(suffix: str) -> str:
    return f"/tmp/u0105-{suffix}"


def _seed_ladder(base_url: str, suffix: str) -> dict[str, str]:
    """Seed LLM provider + agent + workspace + session via the API."""
    ids = {
        "llm": f"j-llm-105-{suffix}",
        "agent": f"j-ag-105-{suffix}",
        "wp": f"j-wp-105-{suffix}",
        "tpl": f"j-tpl-105-{suffix}",
        "workspace": "",
        "session": "",
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": ids["llm"],
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed llm: {r.text}"
        r = c.post("/v1/agents", json={
            "id": ids["agent"],
            "description": "U0105 operator probe",
            "model": agent_model(ids["llm"], "fake-model"),
            "tools": [],
            "system_prompt": ["probe"],
        })
        assert r.status_code == 201, f"seed agent: {r.text}"
        r = c.post("/v1/workspace_providers", json={
            "id": ids["wp"],
            "provider": "local",
            "config": {"kind": "local", "root_path": _container_ws_root(suffix)},
        })
        assert r.status_code == 201, f"seed wp: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": ids["tpl"],
            "description": "u0105 tpl",
            "provider_id": ids["wp"],
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed tpl: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, f"seed ws: {r.text}"
        ids["workspace"] = r.json()["id"]
        r = c.post(
            f"/v1/workspaces/{ids['workspace']}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": ids["agent"]},
                "auto_start": False,
            },
        )
        assert r.status_code == 201, f"seed session: {r.text}"
        ids["session"] = r.json()["id"]
    return ids


def _cleanup(base_url: str, ids: dict[str, str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in (
            f"/v1/workspaces/{ids['workspace']}/sessions/{ids['session']}/cancel"
            if ids.get("session") and ids.get("workspace") else None,
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
# U0105 — Operator troubleshooting journey
# ===========================================================================


def test_u0105_operator_troubleshooting_cross_page_journey(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0105 — Re-pointed: a Studio-native multi-surface troubleshooting
    flow bound to one seeded session + workspace.

    The retired surfaces this used to traverse (the ``/sessions`` list, the
    session-detail References panel with Agent/Workspace anchors, and the
    workspace-detail Sessions tab) are all gone. The Studio consolidates the
    operator's investigation into one in-shell view, so this walks the
    equivalent Studio surfaces:

      1. Seed agent + workspace + session via API.
      2. Enter the workspace's Studio (``#/workspaces/{wid}``).
      3. The workspace-selector sub-header shows the workspace id.
      4. The left-sidebar Sessions section lists the seeded session-row.
      5. Click the row → center tab + agent panel open (panel-agent).
      6. Open the Settings modal → Config section: the reused config
         panel surfaces the workspace id (the "dig into the workspace"
         hop the old Workspace anchor served).
      7. Close settings; the session tab is still open (state coherence
         across the settings hop).

    Cross-surface coherence exercised: rail session-row to center panel,
    plus the workspace-detail overlay, all bound to the same workspace
    and session without leaving the shell.
    """
    ids = _seed_ladder(base_url, unique_suffix)
    sid = ids["session"]
    wid = ids["workspace"]
    try:
        # ----- 1. Enter the Studio ----------------------------------
        open_studio(page, console_url, wid)

        # ----- 2. The topbar names the workspace ---------------------
        # S8 retired the Studio's sub-header workspace-selector; the
        # shell states the workspace id in the topbar instead.
        expect(
            page.get_by_test_id("nv-ws-btn").get_by_text(
                wid, exact=False,
            ).first
        ).to_be_visible(timeout=15_000)

        # ----- 3. Sidebar Sessions section lists the seeded row ------
        # The row renders the session TITLE, not the raw sid — locate it by
        # its data-session-id stamp (the shell rail).
        row = session_row(page, sid, wid)
        expect(row.first).to_be_visible(timeout=20_000)

        # ----- 4. Click the row → center tab + agent panel ----------
        row.first.click()
        expect(page.locator('[data-testid^="nv-tg-tab:"]').first).to_be_visible(
            timeout=15_000,
        )
        expect(page.locator('[data-testid^="nv-session-doc:"]')).to_be_visible(
            timeout=15_000,
        )

        # ----- 5. Workspace settings surface the workspace ----------
        # The workspace menu's "Workspace settings…" opens
        # overlay=workspaces:detail:<wid>, which carries the same
        # config / channels / log / destroy tabs. Opening it in place
        # (no re-navigation) is the point: the open session tab has to
        # survive, which is the coherence check below.
        page.get_by_test_id("nv-ws-btn").click()
        page.get_by_test_id("nv-ws-settings").click()
        overlay = page.get_by_test_id("nv-overlay-body")
        expect(overlay).to_be_visible(timeout=10_000)
        assert f"overlay=workspaces:detail:{wid}" in page.url, (
            f"expected the workspace-detail overlay, got {page.url}"
        )
        # The overlay header is titled by the record it shows.
        expect(page.get_by_text(wid, exact=False).first).to_be_visible(
            timeout=10_000,
        )
        page.get_by_test_id("nv-overlay-close").click()
        expect(overlay).to_have_count(0, timeout=5_000)

        # ----- 6. The session tab survived the settings hop ---------
        expect(page.locator('[data-testid^="nv-session-doc:"]')).to_be_visible(
            timeout=10_000,
        )
    finally:
        _cleanup(base_url, ids)
