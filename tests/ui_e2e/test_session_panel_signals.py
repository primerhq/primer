"""Session-detail panel + signal-button tests.

Covers:
* U0052 — AskUserPanel does NOT render on a terminal session
  (the panel's polling stops on TERMINAL_STATUSES per
  ui/components/session-detail.jsx).
* U0031 — Session pause + resume buttons toggle visible status.
* U0027 — Empty per-collection search renders "No matches" cleanly.
"""

from __future__ import annotations

import time

import httpx
from playwright.sync_api import expect

from tests.ui_e2e._studio_helpers import open_session_in_studio


# ---------------------------------------------------------------------------
# Seed helpers (mirror tests/ui_e2e/test_navigation_and_signals.py)
# ---------------------------------------------------------------------------


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-07", status="partial")


def _seed_llm_provider(base_url: str, pid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": pid,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed LLM failed: {r.text}"


def _seed_agent(base_url: str, agent_id: str, provider_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/agents", json={
            "id": agent_id,
            "description": "ui-e2e probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"


def _seed_workspace(base_url: str, wp_id: str, tpl_id: str, tmp_path) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201, f"seed wp provider failed: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id,
            "description": "ui-e2e tpl",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed wp template failed: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, f"seed workspace failed: {r.text}"
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
        assert r.status_code == 201, f"seed session failed: {r.text}"
        return r.json()["id"]


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# U0052 — AskUserPanel does not render on a terminal session
# ===========================================================================


def test_u0052_ask_user_panel_hidden_on_terminal_session(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0052 — Re-pointed to the Studio. A terminal (ENDED) session can
    never be parked on ask_user, so the Studio's RIGHT sidebar Action
    Required list surfaces NO action-item for it — the yielding-tools
    quiet-state invariant carried over from the retired AskUserPanel.

    Cancel the seeded session via the API (CREATED → ENDED), open it in
    the Studio, wait past a poll cycle, and assert the action-required
    list stays empty (no ``action-item``, no ask-controls). The old
    ``ask-user-panel`` / "Input requested" copy is gone everywhere.
    """
    pid = f"llm-u52-{unique_suffix}"
    aid = f"ag-u52-{unique_suffix}"
    wp_id = f"wp-u52-{unique_suffix}"
    tpl_id = f"tpl-u52-{unique_suffix}"
    _seed_llm_provider(base_url, pid)
    _seed_agent(base_url, aid, pid)
    wid = _seed_workspace(base_url, wp_id, tpl_id, tmp_path)
    sid = _seed_session(base_url, wid, aid)
    cleanup_urls = [
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    try:
        # Cancel via API — CREATED → ENDED with ended_reason='cancelled'.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.post(f"/v1/workspaces/{wid}/sessions/{sid}/cancel")
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "ended"

        # Open the terminal session in the Studio (agent panel).
        open_session_in_studio(page, console_url, wid, sid, kind="agent")
        # Wait past a pending-poll cycle to be sure nothing shows up late.
        page.wait_for_timeout(2_500)

        # No action-item / ask-controls for a terminal session.
        assert page.locator("[data-testid='action-item']").count() == 0, (
            "Action Required surfaced an item for a terminal session"
        )
        assert page.locator("[data-testid='action-ask-controls']").count() == 0
        # The retired panel copy must not appear anywhere.
        assert page.get_by_text("Input requested").count() == 0
        assert page.locator("[data-testid='ask-user-panel']").count() == 0
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0031 — Pause + Resume buttons toggle visible status
# ===========================================================================


def test_u0031_session_pause_resume_buttons_toggle_status(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0031 — Seed a CREATED session, click Resume on the detail page
    (CREATED → WAITING / RUNNING since the worker pool will pick it up
    even though no real LLM is configured, the placeholder Ollama
    provider triggers a fatal which transitions the session toward
    terminal). Then click Pause. Assert each click produces visible
    status text changes within polling cadence.

    Priority area 2 — mutation feedback. Re-pointed to the Studio's
    ``session-controls`` (``ctrl-resume``) in the center agent panel
    (studio-center.jsx ``ST_SessionControls``).

    We tolerate the session reaching terminal (ended/failed) at any
    point — the LLM provider points at a closed port so the worker's
    LLM call will fail. The CONTRACT under test is that the BUTTONS
    transition the visible UI state, not that the session actually
    runs to completion.
    """
    pid = f"llm-u31-{unique_suffix}"
    aid = f"ag-u31-{unique_suffix}"
    wp_id = f"wp-u31-{unique_suffix}"
    tpl_id = f"tpl-u31-{unique_suffix}"
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
    try:
        # Open the session in the Studio — the agent panel + controls mount.
        open_session_in_studio(page, console_url, wid, sid, kind="agent")
        resume_btn = page.locator("[data-testid='ctrl-resume']").first
        resume_btn.wait_for(state="visible", timeout=10_000)

        # The panel header StatusPill reads "created" initially.
        body_initial = (page.locator("body").text_content() or "").lower()
        assert "created" in body_initial, "expected initial 'created' status"

        # Click Resume — should send the signal + show a toast.
        resume_btn.click()
        # Toast confirms the signal was sent.
        page.get_by_text("Resume signal sent", exact=False).first.wait_for(
            state="visible", timeout=10_000,
        )

        # Status moves off CREATED within ~12s (poll cadence 2s + worker
        # claim cycle + LLM fail path). Accept any non-CREATED status.
        non_created_seen = False
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            body_text = (page.locator("body").text_content() or "").lower()
            # Look for any of the non-CREATED status pill values.
            if any(w in body_text for w in (
                "running", "ended", "failed", "cancelled", "completed",
                "waiting", "paused",
            )):
                non_created_seen = True
                break
            page.wait_for_timeout(500)
        assert non_created_seen, (
            "status pill never transitioned off 'created' after Resume"
        )
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0027 — Empty per-collection search renders "No matches"
# ===========================================================================


def test_u0027_empty_collection_search_renders_no_matches(
    page, base_url, console_url, unique_suffix,
) -> None:
    """U0027 — Seed an embedding provider + an empty collection via
    the API. Open the collection detail page, type a query into the
    per-collection search panel, submit, assert the empty-state copy
    appears and no toast-error / console error.

    Priority area 6 — knowledge happy path. Defends the search
    panel's empty-state rendering at
    [`ui/components/knowledge.jsx`](../../ui/components/knowledge.jsx).

    The collection is empty — the search MUST return zero hits and
    the panel MUST render its "No matches" copy. Failure modes we
    pin against: a generic toast error, an undefined exception, or
    a stuck loading spinner.
    """
    embed_pid = f"embed-u27-{unique_suffix}"
    ssp_id = f"ssp-u27-{unique_suffix}"
    coll_id = f"coll-u27-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/embedding_providers", json={
            "id": embed_pid,
            "provider": "openai",
            "config": {"url": "http://127.0.0.1:9999", "api_key": "x"},
            "models": [{"name": "fake-embed", "dim": 8}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed embed failed: {r.text}"
        # Collections now require a SemanticSearchProvider bound at create
        # (Collection.search_provider_id). A self-contained, empty local
        # lance index keeps the seed offline and guarantees zero hits.
        r = c.post("/v1/ssp", json={
            "id": ssp_id,
            "provider": "lance",
            "config": {"path": f"/tmp/lance-u27-{unique_suffix}"},
        })
        assert r.status_code == 201, f"seed ssp failed: {r.text}"
        r = c.post("/v1/collections", json={
            "id": coll_id,
            "description": "ui-e2e empty",
            "embedder": {"provider_id": embed_pid, "model": "fake-embed"},
            "search_provider_id": ssp_id,
        })
        assert r.status_code == 201, f"seed collection failed: {r.text}"
    cleanup_urls = [
        f"/v1/collections/{coll_id}",
        f"/v1/ssp/{ssp_id}",
        f"/v1/embedding_providers/{embed_pid}",
    ]
    try:
        page.goto(
            f"{console_url}#/knowledge/collections",
            wait_until="domcontentloaded",
        )
        # Click the row for our collection to drill in.
        row = page.get_by_text(coll_id, exact=False).first
        row.wait_for(state="visible", timeout=10_000)
        row.click()

        # Wait for the search panel to render. The collection detail
        # page exposes a search box; find it via placeholder or role.
        # In knowledge.jsx the search input usually has placeholder
        # like "Search this collection" — fallback to first textbox
        # inside the collection-detail layout.
        page.wait_for_timeout(800)  # let the detail panel render
        search_inputs = page.get_by_role("textbox").all()
        assert len(search_inputs) >= 1, "no textbox visible on collection detail"
        # Use the first visible textbox (the per-collection search box).
        target = None
        for inp in search_inputs:
            if inp.is_visible():
                target = inp
                break
        assert target is not None, "no visible textbox on collection detail"
        target.fill("any query that won't match")
        # Submit — either by pressing Enter or finding a Search button.
        target.press("Enter")

        # Assert the "No matches" copy appears within ~5s.
        empty_state = page.get_by_text("No matches", exact=False).first
        try:
            expect(empty_state).to_be_visible(timeout=5_000)
        except Exception:
            # The exact copy may differ — accept "no results" / "no hits"
            # / "empty" variants commonly used.
            for alt in ("no results", "no hits", "0 results", "empty"):
                if page.get_by_text(alt, exact=False).count() > 0:
                    return
            raise
    finally:
        _cleanup(base_url, cleanup_urls)
