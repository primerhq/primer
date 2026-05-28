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
        r = c.post("/v1/llm_providers", json={
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
            "model": {"provider_id": ids["llm"], "model_name": "fake-model"},
            "tools": [],
            "system_prompt": ["probe"],
        })
        assert r.status_code == 201, f"seed agent: {r.text}"
        r = c.post("/v1/workspace_providers", json={
            "id": ids["wp"],
            "provider": "local",
            "config": {"kind": "local", "path": _container_ws_root(suffix)},
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
    """U0105 — Multi-page operator troubleshooting flow.

    Steps:

      1. Seed agent + workspace + session via API.
      2. Navigate /sessions list — assert seeded session row visible.
      3. Click the row → /sessions/{sid} detail.
      4. Locate the References panel — assert Agent + Workspace
         ref-rows render.
      5. Click the Agent anchor → /agents/{aid}.
      6. Verify the agent detail page renders the seeded id.
      7. Browser back → /sessions/{sid} (the same session, not a
         fresh load — back-stack preserves state).
      8. Click the Workspace anchor → /workspaces/{wid}.
      9. Verify workspace detail header renders the wid.
     10. Click the Sessions tab → see the seeded session row in the
         workspace's Sessions tab (exercises the SessionInfo field
         fix from commit 505e76e — without it the row's Session
         column would be blank).
     11. Click the workspace Sessions tab row → /sessions/{sid}.

    Pages visited (in order):
      /console/ → /sessions → /sessions/{sid} → /agents/{aid}
      → /sessions/{sid} (back) → /workspaces/{wid}?tab=files
      → /workspaces/{wid}?tab=sessions → /sessions/{sid}

    Cross-page links exercised: References panel's Agent anchor,
    References panel's Workspace anchor, workspace Sessions-tab row.
    """
    ids = _seed_ladder(base_url, unique_suffix)
    sid = ids["session"]
    aid = ids["agent"]
    wid = ids["workspace"]
    try:
        # ----- 1. /sessions list ------------------------------------
        page.goto(f"{console_url}#/sessions", wait_until="domcontentloaded")
        expect(page.locator("h1.page-title")).to_have_text(
            "Sessions", timeout=20_000,
        )
        row = page.locator("tbody tr", has_text=sid)
        expect(row).to_be_visible(timeout=20_000)

        # ----- 2. /sessions/{sid} detail ----------------------------
        row.first.click()
        page.wait_for_url(f"**/console/#/sessions/{sid}", timeout=15_000)
        expect(page.locator("h1.page-title", has_text=sid)).to_be_visible(
            timeout=20_000,
        )

        # References panel renders. Both Agent + Workspace ref-rows
        # should be present (auto_start=False so worker may or may
        # not have claimed yet; we don't depend on the Worker ref-row).
        ref_panel = page.locator(".panel", has_text="References")
        expect(ref_panel).to_be_visible(timeout=10_000)
        agent_ref = ref_panel.locator(".ref-row", has_text="Agent")
        ws_ref = ref_panel.locator(".ref-row", has_text="Workspace")
        expect(agent_ref).to_be_visible(timeout=5_000)
        expect(ws_ref).to_be_visible(timeout=5_000)

        # ----- 3. Click Agent anchor → /agents/{aid} -----------------
        # The clickable element is the <a> inside the ref-row's .val
        # span; aid appears as the link text.
        agent_ref.locator("a").click()
        page.wait_for_url(f"**/console/#/agents/{aid}**", timeout=15_000)
        expect(page.locator("h1.page-title", has_text=aid)).to_be_visible(
            timeout=15_000,
        )

        # ----- 4. Browser back → back to session detail -------------
        page.go_back()
        page.wait_for_url(f"**/console/#/sessions/{sid}", timeout=15_000)
        expect(page.locator("h1.page-title", has_text=sid)).to_be_visible(
            timeout=15_000,
        )

        # ----- 5. Click Workspace anchor → /workspaces/{wid} ---------
        # Re-resolve the locator since the page was re-rendered after
        # back-nav (React re-mounts on hash change).
        ref_panel = page.locator(".panel", has_text="References")
        ws_ref = ref_panel.locator(".ref-row", has_text="Workspace")
        ws_ref.locator("a").click()
        page.wait_for_url(f"**/console/#/workspaces/{wid}**", timeout=15_000)
        expect(page.locator("h1.page-title", has_text=wid)).to_be_visible(
            timeout=15_000,
        )

        # ----- 6. Click Sessions tab → seeded session row visible ---
        # SessionsTab polls /v1/workspaces/{wid}/sessions every 5s;
        # row should be visible within ~10s.
        page.get_by_role("button", name="Sessions").click()
        # Wait for tab transition + row poll.
        ws_row = page.locator("tbody tr", has_text=sid)
        expect(ws_row).to_be_visible(timeout=20_000)

        # ----- 7. Click workspace Sessions row → /sessions/{sid} ----
        ws_row.first.click()
        page.wait_for_url(f"**/console/#/sessions/{sid}", timeout=15_000)
        expect(page.locator("h1.page-title", has_text=sid)).to_be_visible(
            timeout=15_000,
        )
    finally:
        _cleanup(base_url, ids)
