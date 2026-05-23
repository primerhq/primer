"""AskUserPanel UI tests — polling + interaction edge cases.

Uses Playwright page.route to mock /v1/sessions/{sid}/ask_user/pending
(pattern from commit 5a9b849).

Covers backlog items:
* U0055 — Multi-line prompt renders textarea, not input.
* U0059 — Visible prompt text updates when polled prompt changes.
* U0063 — Enter key in short-prompt input submits the response.
* U0066 — Skip button disabled while a submit is in-flight.
"""

from __future__ import annotations

import json

import httpx
from playwright.sync_api import expect


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
            "description": "ask_user polling probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"


def _seed_workspace(base_url: str, wp_id: str, tpl_id: str, tmp_path) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "path": str(tmp_path)},
        })
        assert r.status_code == 201, f"seed wp provider failed: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id, "description": "ws tpl",
            "provider_id": wp_id, "backend": {"kind": "local"},
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


def _seed_ladder(base_url: str, unique_suffix: str, tmp_path):
    pid = f"llm-up-{unique_suffix}"
    aid = f"ag-up-{unique_suffix}"
    wp_id = f"wp-up-{unique_suffix}"
    tpl_id = f"tpl-up-{unique_suffix}"
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
    return sid, cleanup_urls


def _pending_body(
    *, tool_call_id="tc-p", prompt, response_schema=None,
) -> str:
    return json.dumps({
        "tool_call_id": tool_call_id,
        "prompt": prompt,
        "response_schema": response_schema,
        "parked_at": "2026-05-23T12:00:00+00:00",
    })


# ===========================================================================
# U0055 — Multi-line prompt renders textarea, not input
# ===========================================================================


def test_u0055_multi_line_prompt_renders_textarea_not_input(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0055 — Even a short prompt with a newline must take the
    textarea branch (heuristic: ``!prompt.includes("\\n") &&
    prompt.length <= 80``).
    """
    sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        # Short by length (well under 80) but has a newline.
        prompt = "Multi-line?\nplease elaborate"
        assert len(prompt) <= 80
        assert "\n" in prompt
        page.route(
            f"**/v1/sessions/{sid}/ask_user/pending",
            lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=_pending_body(prompt=prompt),
            ),
        )
        page.goto(
            f"{console_url}#/sessions/{sid}", wait_until="domcontentloaded",
        )
        expect(
            page.locator("[data-testid='ask-user-panel']")
        ).to_be_visible(timeout=10_000)
        # Textarea variant, input absent.
        assert page.locator("[data-testid='ask-user-textarea']").count() == 1
        assert page.locator("[data-testid='ask-user-input']").count() == 0
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0059 — Polled prompt update reflects new text within polling cadence
# ===========================================================================


def test_u0059_polled_prompt_text_updates_when_pending_changes(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0059 — A second prompt's text replaces the first within the
    2s polling cadence. Defends the panel against rendering stale
    cached prompts.
    """
    sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        # Mutable state to swap the prompt mid-test.
        state = {"prompt": "first?"}

        def _on_pending(route):
            route.fulfill(
                status=200, content_type="application/json",
                body=_pending_body(prompt=state["prompt"]),
            )

        page.route(
            f"**/v1/sessions/{sid}/ask_user/pending", _on_pending,
        )
        page.goto(
            f"{console_url}#/sessions/{sid}", wait_until="domcontentloaded",
        )
        panel = page.locator("[data-testid='ask-user-panel']")
        expect(panel).to_be_visible(timeout=10_000)
        expect(panel).to_contain_text("first?")

        # Flip the route's response to a new prompt.
        state["prompt"] = "second!"
        # Next poll happens within ~2s; budget 6s.
        expect(panel).to_contain_text("second!", timeout=6_000)
        # And the first prompt is gone.
        assert "first?" not in (panel.text_content() or "")
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0063 — Enter key in short-prompt input submits the response
# ===========================================================================


def test_u0063_enter_key_in_short_prompt_input_submits_response(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0063 — The short-prompt input variant binds Enter (without
    Shift) to onSubmit per session-detail.jsx. Pins that keyboard
    affordance.
    """
    sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        page.route(
            f"**/v1/sessions/{sid}/ask_user/pending",
            lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=_pending_body(prompt="Short?"),
            ),
        )
        respond_calls: list[dict] = []

        def _on_respond(route):
            respond_calls.append(
                {"body": route.request.post_data}
            )
            route.fulfill(
                status=202, content_type="application/json",
                body=json.dumps({"status": "accepted"}),
            )

        page.route(
            f"**/v1/sessions/{sid}/ask_user/respond", _on_respond,
        )

        page.goto(
            f"{console_url}#/sessions/{sid}", wait_until="domcontentloaded",
        )
        expect(
            page.locator("[data-testid='ask-user-panel']")
        ).to_be_visible(timeout=10_000)

        # Fill the input and press Enter.
        inp = page.locator("[data-testid='ask-user-input']")
        inp.fill("Alice")
        inp.press("Enter")

        # Toast appears.
        expect(
            page.get_by_text("Response sent", exact=False)
        ).to_be_visible(timeout=5_000)
        # /respond was hit with the right body.
        assert len(respond_calls) >= 1, "respond endpoint was not called"
        body = json.loads(respond_calls[-1]["body"] or "{}")
        assert body.get("response") == "Alice", body
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0066 — Skip button disabled while a submit is in-flight
# ===========================================================================


def test_u0066_skip_button_disabled_while_submit_in_flight(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0066 — Both Send and Skip are gated on
    ``submitting || skipping`` per session-detail.jsx. While a
    submit is in flight (route handler delays the 202 response),
    the Skip button must report disabled — otherwise the operator
    could send two conflicting signals.
    """
    sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        page.route(
            f"**/v1/sessions/{sid}/ask_user/pending",
            lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=_pending_body(prompt="Short?"),
            ),
        )
        # Delay /respond by 2s so we have a wide mid-flight window
        # for the Skip-disabled assertion.
        import time as _time

        def _on_respond(route):
            _time.sleep(2.0)
            route.fulfill(
                status=202, content_type="application/json",
                body=json.dumps({"status": "accepted"}),
            )

        page.route(
            f"**/v1/sessions/{sid}/ask_user/respond", _on_respond,
        )

        page.goto(
            f"{console_url}#/sessions/{sid}", wait_until="domcontentloaded",
        )
        expect(
            page.locator("[data-testid='ask-user-panel']")
        ).to_be_visible(timeout=10_000)

        # Pre-click: Skip is enabled.
        skip = page.get_by_role("button", name="Skip this prompt")
        expect(skip).to_be_enabled()

        # Fill + click Send. Don't await the click's full settle —
        # send_request is fire-and-forget on the button-click handler.
        page.locator("[data-testid='ask-user-input']").fill("Bob")
        # Use no_wait_after to avoid Playwright's auto-wait racing the
        # response handler.
        page.get_by_role(
            "button", name="Send response",
        ).click(no_wait_after=True)

        # Mid-flight assertion: Skip becomes disabled (poll quickly).
        # The 2s server delay gives a wide window.
        expect(skip).to_be_disabled(timeout=1_500)

        # After completion, the toast appears + Skip becomes enabled
        # again (panel collapse would also happen if /pending flipped,
        # but we didn't flip it — so the panel stays open).
        expect(
            page.get_by_text("Response sent", exact=False)
        ).to_be_visible(timeout=5_000)
    finally:
        _cleanup(base_url, cleanup_urls)
