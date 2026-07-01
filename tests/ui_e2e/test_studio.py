"""UI E2E: the workspace Studio (PR-B) — the IDE-style view at /workspaces/:wid.

Gated: collected-then-ignored unless ``PRIMER_RUN_UI_E2E=1``, exactly
like every other test in this directory (see conftest.py:collect_ignore_glob)
and mirroring test_workspace_tap_events.py's explicit per-file gate too.

The Studio (B1-B5) replaces the old Sessions list + session-detail pages.
B6 retired the ``/sessions`` and ``/sessions/:id`` routes into redirects
into the Studio. This module pins the load-bearing flows:

  1. Navigating to a workspace renders the Studio shell — all three region
     wrappers (``studio-sidebar`` / ``studio-center`` / ``studio-activity``)
     mount.
  2. The left sidebar lists the workspace's sessions (and, when a file was
     seeded, its files).
  3. Clicking a session row opens a center tab and the agent panel
     (``panel-agent`` for an agent-bound session).
  4. Opening a file row shows the file panel (``panel-file``) in preview.
  5. ``⌘K`` opens the command palette (``command-palette``).
  6. The redirect: navigating to ``#/sessions/<sid>`` lands in the Studio
     with that session's tab open (``panel-agent``).

Flows are driven via Playwright against a live server seeded through a
sync httpx client (mirroring test_session_lifecycle_journey.py's ladder
and test_workspace_file_download_journey.py's file PUT). Seeding is
skip-soft on 5xx — the primer-app container may not reach the workspace
provider's host path (U0072/U0080-class), in which case we skip rather
than fail.

Ladder: llm_provider → workspace_provider → workspace_template →
        workspace → agent → session (auto_start=False so it parks in
        CREATED) → optional file PUT.
"""

from __future__ import annotations

import os

import httpx
import pytest
from playwright.sync_api import expect


from tests._support.smk import smk  # noqa: E402

pytestmark = smk("SMK-UI-06")


# ---------------------------------------------------------------------------
# Gate — mirrors conftest.py's collect_ignore_glob mechanism
# ---------------------------------------------------------------------------

if os.environ.get("PRIMER_RUN_UI_E2E") != "1":
    collect_ignore_glob = ["test_studio.py"]


# ---------------------------------------------------------------------------
# Container-internal workspace provider path — host tmp_path is not visible
# from the primer-app container; /tmp/<suffix> inside the container avoids
# the U0072/U0080-class host-path unreachability problem.
# ---------------------------------------------------------------------------


def _container_ws_root(suffix: str) -> str:
    return f"/tmp/studio-{suffix}"


def _seed_studio_ladder(base_url: str, suffix: str) -> dict[str, str]:
    """Seed llm_provider → workspace_provider → template → workspace →
    agent → session via the API. Returns the ids.

    Session is created with auto_start=False so it parks in CREATED
    indefinitely (no LM Studio dependency); the row is still listed and
    openable in the Studio sidebar.
    """
    ids = {
        "llm": f"st-llm-{suffix}",
        "wp": f"st-wp-{suffix}",
        "tpl": f"st-tpl-{suffix}",
        "agent": f"st-ag-{suffix}",
        "workspace": "",  # backend-assigned
        "session": "",  # backend-assigned
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
            "description": "studio template",
            "provider_id": ids["wp"],
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed tpl failed: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        if r.status_code >= 500:
            pytest.skip(
                f"workspace create returned {r.status_code} — primer-app "
                f"container likely can't reach host tmp (U0072/U0080-class)."
            )
        assert r.status_code == 201, f"seed workspace failed: {r.text}"
        ids["workspace"] = r.json()["id"]
        r = c.post("/v1/agents", json={
            "id": ids["agent"],
            "description": "studio agent",
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


def _seed_file(base_url: str, wid: str, name: str, content: str) -> bool:
    """PUT one text file into the workspace. Returns True on success,
    False if the container can't reach the provider path (skip-soft)."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.put(
            f"/v1/workspaces/{wid}/files?path={name}",
            json={"content": content, "encoding": "text"},
        )
        if r.status_code >= 500:
            return False
        assert r.status_code in (200, 201, 204), r.text
        return True


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
# Studio shell + sidebar + center + palette
# ===========================================================================


@pytest.mark.skipif(
    os.environ.get("PRIMER_RUN_UI_E2E") != "1",
    reason="Set PRIMER_RUN_UI_E2E=1 to run UI e2e tests",
)
def test_studio_shell_sidebar_center_and_palette(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    console_messages: list[dict],
) -> None:
    """Navigate to a workspace → the Studio shell mounts (3 regions), the
    sidebar lists the seeded session + file, clicking the session row opens
    a center tab + the agent panel, opening the file row shows the file
    panel in preview, and ⌘K opens the command palette."""

    ids = _seed_studio_ladder(base_url, unique_suffix)
    wid = ids["workspace"]
    file_name = f"studio-{unique_suffix}.txt"
    file_marker = f"studio-marker-{unique_suffix}"
    have_file = _seed_file(
        base_url, wid, file_name, f"{file_marker}\nline 2\nline 3\n",
    )

    try:
        # --- 1. Studio shell: all three regions mount ------------------
        page.goto(
            f"{console_url}#/workspaces/{wid}",
            wait_until="domcontentloaded",
        )
        expect(page.locator('[data-testid="studio-root"]')).to_be_visible(
            timeout=20_000,
        )
        for region in ("studio-sidebar", "studio-center", "studio-activity"):
            expect(page.locator(f'[data-testid="{region}"]')).to_be_visible(
                timeout=10_000,
            )

        # --- 2. Left sidebar lists the seeded session ------------------
        session_row = page.locator('[data-testid="session-row"]').first
        expect(session_row).to_be_visible(timeout=20_000)

        # --- 3. Click session row → center tab + agent panel ----------
        session_row.click()
        expect(page.locator('[data-testid="center-tab"]').first).to_be_visible(
            timeout=15_000,
        )
        # The seeded session is agent-bound → the agent panel renders.
        expect(page.locator('[data-testid="panel-agent"]')).to_be_visible(
            timeout=15_000,
        )

        # --- 4. Open the file row → file panel in preview --------------
        # The Files section defaults to open (studio.jsx filesOpen: true,
        # no persisted state in a fresh browser context), so the seeded
        # file surfaces directly as a file-row.
        if have_file:
            file_row = page.locator(
                '[data-testid="file-row"]', has_text=file_name,
            ).first
            expect(file_row).to_be_visible(timeout=20_000)
            file_row.click()
            expect(page.locator('[data-testid="panel-file"]')).to_be_visible(
                timeout=15_000,
            )
            # Preview is the default mode; the breadcrumb shows the path.
            expect(
                page.locator('[data-testid="file-breadcrumb"]')
            ).to_contain_text(file_name, timeout=10_000)

        # --- 5. ⌘K opens the command palette ---------------------------
        # The Studio keydown handler binds `e.metaKey || e.ctrlKey` + "k";
        # on Linux Chromium (the e2e browser) Control is the reliable
        # modifier (Meta maps to the Super key).
        page.keyboard.press("Control+k")
        palette = page.locator('[data-testid="command-palette"]')
        expect(palette).to_be_visible(timeout=10_000)
        # Close it again so it doesn't swallow subsequent input.
        page.keyboard.press("Escape")

        # --- 6. No console errors across the whole flow ----------------
        # The Studio renders IN-SHELL, so the app Topbar's pre-existing
        # GET /v1/internal_collections/config probe (404 when the feature
        # is inactive — the e2e default) is now visible here. It is an
        # app-shell fetch, not a Studio bug, so it rides the allowlist.
        from tests.ui_e2e.conftest import assert_no_console_errors
        from tests.ui_e2e._studio_helpers import STUDIO_CONSOLE_IGNORES
        assert_no_console_errors(
            console_messages,
            ignore_patterns=STUDIO_CONSOLE_IGNORES,
        )

    finally:
        _cleanup(base_url, ids)


@pytest.mark.skipif(
    os.environ.get("PRIMER_RUN_UI_E2E") != "1",
    reason="Set PRIMER_RUN_UI_E2E=1 to run UI e2e tests",
)
def test_studio_session_redirect_lands_with_tab_open(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    console_messages: list[dict],
) -> None:
    """B6 redirect: navigating to the retired ``#/sessions/<sid>`` route
    resolves the session's workspace and lands in the Studio with that
    session's tab open (agent panel rendered)."""

    ids = _seed_studio_ladder(base_url, "rdr-" + unique_suffix)
    wid = ids["workspace"]
    sid = ids["session"]

    try:
        # The old session-detail route now redirects into the Studio.
        page.goto(
            f"{console_url}#/sessions/{sid}",
            wait_until="domcontentloaded",
        )
        # The redirect resolves the workspace and replaces the hash.
        page.wait_for_url(f"**/console/#/workspaces/{wid}**", timeout=20_000)
        # The Studio shell mounts with the session tab open → agent panel.
        expect(page.locator('[data-testid="studio-root"]')).to_be_visible(
            timeout=20_000,
        )
        expect(page.locator('[data-testid="center-tab"]').first).to_be_visible(
            timeout=15_000,
        )
        expect(page.locator('[data-testid="panel-agent"]')).to_be_visible(
            timeout=15_000,
        )

        # The B6 redirect lands in-shell, so the app Topbar's pre-existing
        # GET /v1/internal_collections/config 404 (feature inactive) is now
        # visible here. Allowlist it — it is an app-shell fetch, not a
        # Studio/redirect bug.
        from tests.ui_e2e.conftest import assert_no_console_errors
        from tests.ui_e2e._studio_helpers import STUDIO_CONSOLE_IGNORES
        assert_no_console_errors(
            console_messages,
            ignore_patterns=STUDIO_CONSOLE_IGNORES,
        )

    finally:
        _cleanup(base_url, ids)
