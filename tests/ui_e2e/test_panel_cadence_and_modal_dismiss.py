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
            "config": {"kind": "local", "path": str(tmp_path)},
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


def test_u0057_waiting_since_renders_parked_at_timestamp(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0057 — When the panel renders, the parked_at timestamp from
    the pending response appears next to the header as "waiting
    since {fmtDate(...)}". Pins the affordance against a regression
    that drops the parked_at display.
    """
    _, sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        parked_at = "2026-05-23T14:30:00+00:00"
        page.route(
            f"**/v1/sessions/{sid}/ask_user/pending",
            lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({
                    "tool_call_id": "tc-w",
                    "prompt": "Short?",
                    "response_schema": None,
                    "parked_at": parked_at,
                }),
            ),
        )

        page.goto(
            f"{console_url}#/sessions/{sid}", wait_until="domcontentloaded",
        )
        panel = page.locator("[data-testid='ask-user-panel']")
        expect(panel).to_be_visible(timeout=10_000)
        # "waiting since" text plus a recognisable date/time fragment.
        # fmtDate(new Date(parked_at)) renders a locale-formatted
        # string — we don't pin the exact format, just that
        # "waiting since" is present and at least one date/time
        # fragment (year 2026 or hour digits) follows.
        panel_text = panel.text_content() or ""
        assert "waiting since" in panel_text, (
            f"expected 'waiting since' in panel, got {panel_text!r}"
        )
        # Look for the year 2026 from the timestamp, or any 14:30
        # (UTC hour-minute) variant. fmtDate may render local TZ, so
        # accept either year fragment OR a hh:mm pattern.
        import re
        has_year = "2026" in panel_text
        has_time = bool(re.search(r"\b\d{1,2}:\d{2}\b", panel_text))
        assert has_year or has_time, (
            f"no date/time fragment after 'waiting since': {panel_text!r}"
        )
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0064 — Panel polls /ask_user/pending ~every 2s while non-terminal
# ===========================================================================


def test_u0064_panel_polls_pending_endpoint_while_non_terminal(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0064 — The panel's useResource polls every 2s while the
    session is non-terminal. Mock /ask_user/pending → 404 with a
    counter; over ~7s we should see at least 3 hits.
    """
    _, sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        hits = {"count": 0}

        def _on_pending(route):
            hits["count"] += 1
            route.fulfill(
                status=404, content_type="application/json",
                body=json.dumps({
                    "type": "/errors/not-found",
                    "title": "Not Found",
                    "status": 404,
                    "detail": "no pending",
                }),
            )

        page.route(
            f"**/v1/sessions/{sid}/ask_user/pending", _on_pending,
        )

        page.goto(
            f"{console_url}#/sessions/{sid}", wait_until="domcontentloaded",
        )
        # Wait for chrome to mount + session to load via the Resume
        # button (resilient gate vs CDN flakes).
        page.locator(".nav-item").first.wait_for(
            state="visible", timeout=20_000,
        )
        page.get_by_role(
            "button", name="Resume", exact=True,
        ).first.wait_for(state="visible", timeout=10_000)

        # Now observe ~7s of polling. The panel polls every 2s while
        # non-terminal (CREATED here, since auto_start=False).
        page.wait_for_timeout(7_500)
        # Expect at least 3 calls (initial + ~3 polls).
        assert hits["count"] >= 3, (
            f"expected ≥3 /ask_user/pending hits in ~7s, got {hits['count']}"
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


def test_u0069_cancel_modal_esc_dismiss_does_not_signal(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0069 — Clicking the page-level Cancel button opens a
    confirmation modal. Pressing ESC must close it without firing
    the cancel signal — the session row stays CREATED, and no
    "Cancel signal sent" toast appears.

    Pins the safety contract: the destructive action is gated
    behind explicit confirmation; backing out is harmless.
    """
    _, sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        # Track whether /cancel was actually called.
        cancel_calls = {"count": 0}

        def _on_cancel_signal(route):
            cancel_calls["count"] += 1
            route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"id": sid, "status": "ended"}),
            )

        # Match the workspace-nested cancel endpoint.
        page.route(
            "**/v1/workspaces/*/sessions/*/cancel", _on_cancel_signal,
        )

        page.goto(
            f"{console_url}#/sessions/{sid}", wait_until="domcontentloaded",
        )
        # Resilience gate.
        page.locator(".nav-item").first.wait_for(
            state="visible", timeout=20_000,
        )
        # Wait for the page-level Cancel button (one of the signal buttons).
        cancel_btn = page.get_by_role(
            "button", name="Cancel", exact=True,
        ).first
        cancel_btn.wait_for(state="visible", timeout=10_000)
        cancel_btn.click()

        # Confirmation modal: "Cancel session?" title + "Keep running"
        # + "Cancel session" buttons.
        page.get_by_text("Cancel session?", exact=False).first.wait_for(
            state="visible", timeout=5_000,
        )

        # ESC dismiss.
        page.keyboard.press("Escape")
        # Modal closes — wait for "Cancel session?" text to leave the
        # DOM (or just become hidden).
        page.wait_for_timeout(500)
        # The modal title shouldn't be visible anymore.
        assert page.get_by_text("Cancel session?", exact=False).count() == 0, (
            "Cancel confirmation modal didn't dismiss on ESC"
        )

        # /cancel was NOT called.
        assert cancel_calls["count"] == 0, (
            f"cancel signal fired despite ESC dismiss: "
            f"{cancel_calls['count']} call(s)"
        )

        # No "Cancel signal sent" toast.
        assert page.get_by_text(
            "Cancel signal sent", exact=False,
        ).count() == 0, "Cancel toast fired despite ESC dismiss"
    finally:
        _cleanup(base_url, cleanup_urls)
