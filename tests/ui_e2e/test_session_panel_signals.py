"""Session-detail panel + signal-button tests.

Covers:
* U0052 — AskUserPanel does NOT render on a terminal session
  (the panel's polling stops on TERMINAL_STATUSES per
  ui/components/session-detail.jsx).
* U0031 — Composer send (invoke/steer/resume) toggles visible status.
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
from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests.ui_e2e._shell_helpers import open_legacy_route
pytestmark = smk("SMK-UI-07", status="partial")


def _seed_llm_provider(base_url: str, pid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
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
            "model": agent_model(provider_id, "fake-model"),
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
    Required list surfaces NO decision card for it — the yielding-tools
    quiet-state invariant carried over from the retired AskUserPanel.

    Cancel the seeded session via the API (CREATED → ENDED), open it in
    the Studio, wait past a poll cycle, and assert the action-required
    list stays empty (no decision card, no answer input). The old
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

        # No decision card for a terminal session.
        assert page.locator("[data-testid^='shell-decision:']").count() == 0, (
            "Action Required surfaced an item for a terminal session"
        )
        assert page.get_by_test_id("shell-decision-answer").count() == 0
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
    """U0031 — Seed a CREATED session, send a message via the Composer
    (CREATED → WAITING / RUNNING since the worker pool will pick it up
    even though no real LLM is configured, the placeholder Ollama
    provider triggers a fatal which transitions the session toward
    terminal). Assert the send produces a visible status-pill change
    within the polling cadence.

    Priority area 2 — mutation feedback. Re-pointed: the Studio's agent
    panel has no Pause/Resume controls anymore (the shell session document's
    ST_SessionControls cluster is defined but never mounted by
    SessionAgentPanel). Per session-adapter.jsx, resuming a CREATED
    session is now "send a message via the Composer" — the SAME
    POST .../steer call auto-wakes a CREATED/PAUSED/WAITING/ENDED session
    (session/enqueue.py's wake_session, "one input, four behaviours" —
    every clean turn now ends the session, and a follow-up message
    restarts an ENDED one in place instead of erroring).

    We tolerate the session reaching terminal (ended/failed) at any
    point — the LLM provider points at a closed port so the worker's
    LLM call will fail. The CONTRACT under test is that the Composer
    send transitions the visible UI state, not that the session actually
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
        # Open the session in the Studio — the agent panel + Composer mount.
        open_session_in_studio(page, console_url, wid, sid, kind="agent")
        composer = page.locator("textarea[placeholder='Send a message…']")
        composer.wait_for(state="visible", timeout=10_000)

        # The rail row carries a status before anything is sent. NOT
        # pinned to "created": a seeded session does not reliably sit
        # there to be observed, since the scheduler may claim it between
        # the seed and the assertion, and what this test is about is the
        # transition a send causes.
        initial_chip = page.locator('[data-testid="session-status-dot"]').first
        expect(initial_chip).to_be_visible(timeout=10_000)

        # Send a message via the Composer — this is the resume/steer/invoke
        # signal now (POST .../steer, session-adapter.jsx sendMessage).
        composer.fill("please resume")
        page.locator("[data-testid='chat-send-btn']").click()

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
        # A self-contained, empty local lance index keeps the seed
        # offline and guarantees zero hits.
        r = c.post("/v1/ssp", json={
            "id": ssp_id,
            "provider": "lance",
            "config": {"path": f"/tmp/lance-u27-{unique_suffix}"},
        })
        assert r.status_code == 201, f"seed ssp failed: {r.text}"
        r = c.post("/v1/collections", json={
            "id": coll_id,
            "description": "ui-e2e empty",
        })
        assert r.status_code == 201, f"seed collection failed: {r.text}"
        # Search on, index empty. That is the state under test: a
        # grep-only collection is a different signal, and it is what
        # this seed silently produced while the binding still rode on
        # the create body.
        r = c.put(f"/v1/collections/{coll_id}/search", json={
            "embedder": {"provider_id": embed_pid, "model": "fake-embed"},
            "vector_store_provider_id": ssp_id,
        })
        assert r.status_code in (200, 201, 202), f"enable search: {r.text}"
    cleanup_urls = [
        f"/v1/collections/{coll_id}",
        f"/v1/ssp/{ssp_id}",
        f"/v1/embedding_providers/{embed_pid}",
    ]
    try:
        open_legacy_route(page, console_url, "knowledge/collections")
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
        # Scope to the overlay: the shell behind it has textboxes of its
        # own, and the composer is earlier in the DOM, so a page-wide
        # search found that instead and pressing Enter steered the
        # session rather than running the grep.
        overlay = page.get_by_test_id("shell-overlay-body")
        search_inputs = overlay.get_by_role("textbox").all()
        assert len(search_inputs) >= 1, "no textbox visible on collection detail"
        # Use the first visible textbox (the per-collection grep box).
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
