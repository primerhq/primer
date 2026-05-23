"""AskUserPanel UI tests — variant rendering + input edge cases.

Mocks GET /v1/sessions/{sid}/ask_user/pending via Playwright
page.route so the panel can render without a real LLM-driven park.
Pattern established in commit 5a9b849 (tests/ui_e2e/test_ask_user_panel.py).

Covers backlog items:
* U0054 — Long prompt (>80 chars) renders textarea, not input.
* U0056 — Send disabled while input is empty or whitespace-only.
* U0061 — JSON-object schema renders mono textarea + JSON placeholder.
* U0062 — Client-side JSON parse error renders inline before any
  /respond POST.
"""

from __future__ import annotations

import json

import httpx
from playwright.sync_api import expect


# ---------------------------------------------------------------------------
# Seed helpers (copied from test_ask_user_panel.py for self-containment)
# ---------------------------------------------------------------------------


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
            "description": "ask_user variant probe",
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
            "config": {"kind": "local", "path": str(tmp_path)},
        })
        assert r.status_code == 201, f"seed wp provider failed: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id,
            "description": "ui-e2e ws tpl",
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


def _seed_ladder(base_url: str, unique_suffix: str, tmp_path):
    pid = f"llm-uv-{unique_suffix}"
    aid = f"ag-uv-{unique_suffix}"
    wp_id = f"wp-uv-{unique_suffix}"
    tpl_id = f"tpl-uv-{unique_suffix}"
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


def _route_pending(
    page, sid, *, tool_call_id="tc-v", prompt, response_schema=None,
):
    body = {
        "tool_call_id": tool_call_id,
        "prompt": prompt,
        "response_schema": response_schema,
        "parked_at": "2026-05-23T12:00:00+00:00",
    }
    page.route(
        f"**/v1/sessions/{sid}/ask_user/pending",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(body),
        ),
    )


# ===========================================================================
# U0054 — Long prompt (>80 chars) renders textarea, not input
# ===========================================================================


def test_u0054_long_prompt_renders_textarea_not_input(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0054 — The panel's heuristic (per session-detail.jsx) renders
    ``<input>`` for prompts ≤ 80 chars AND single-line; everything
    else gets ``<textarea>``. Long prompts must take the textarea
    branch so the operator has room to read + reply.
    """
    sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        long_prompt = (
            "This is a deliberately long prompt that exceeds the "
            "single-line eighty-character heuristic threshold by a comfortable margin."
        )
        assert len(long_prompt) > 80, "test fixture must exceed 80 chars"
        _route_pending(page, sid, prompt=long_prompt)
        page.goto(
            f"{console_url}#/sessions/{sid}", wait_until="domcontentloaded",
        )
        expect(
            page.locator("[data-testid='ask-user-panel']")
        ).to_be_visible(timeout=10_000)
        # Textarea branch — input must NOT be present.
        assert page.locator("[data-testid='ask-user-textarea']").count() == 1
        assert page.locator("[data-testid='ask-user-input']").count() == 0
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0056 — Send button disabled when input is empty / whitespace-only
# ===========================================================================


def test_u0056_send_button_disabled_for_empty_and_whitespace_input(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0056 — The panel's Send button is gated on draft.trim() (per
    session-detail.jsx). At mount the input is empty → Send disabled.
    Filling whitespace-only keeps Send disabled. Filling a real
    character enables Send.
    """
    sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        _route_pending(page, sid, prompt="Short?")  # input variant
        page.goto(
            f"{console_url}#/sessions/{sid}", wait_until="domcontentloaded",
        )
        expect(
            page.locator("[data-testid='ask-user-panel']")
        ).to_be_visible(timeout=10_000)

        send = page.get_by_role("button", name="Send response")
        # At mount: empty draft → disabled.
        expect(send).to_be_disabled()

        # Whitespace only → still disabled.
        page.locator("[data-testid='ask-user-input']").fill("   ")
        expect(send).to_be_disabled()

        # Real character → enabled.
        page.locator("[data-testid='ask-user-input']").fill("a")
        expect(send).to_be_enabled()

        # Clear again → disabled.
        page.locator("[data-testid='ask-user-input']").fill("")
        expect(send).to_be_disabled()
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0061 — JSON-object schema renders mono textarea + JSON placeholder
# ===========================================================================


def test_u0061_object_schema_renders_mono_textarea_with_json_placeholder(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0061 — When response_schema.type == "object" the panel
    switches to the textarea variant with the ``mono`` class and a
    placeholder hinting that JSON is expected. Pins the schema-aware
    rendering branch in session-detail.jsx (className composition +
    placeholder).
    """
    sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        _route_pending(
            page, sid, prompt="Provide config", response_schema=schema,
        )
        page.goto(
            f"{console_url}#/sessions/{sid}", wait_until="domcontentloaded",
        )
        expect(
            page.locator("[data-testid='ask-user-panel']")
        ).to_be_visible(timeout=10_000)

        textarea = page.locator("[data-testid='ask-user-textarea']")
        expect(textarea).to_be_visible()
        # Assert mono class is on the textarea.
        cls = textarea.get_attribute("class") or ""
        assert "mono" in cls, f"expected 'mono' in className, got {cls!r}"
        # Placeholder mentions JSON.
        placeholder = textarea.get_attribute("placeholder") or ""
        assert "JSON" in placeholder, (
            f"expected 'JSON' in placeholder, got {placeholder!r}"
        )
        # And the input variant is NOT rendered.
        assert page.locator("[data-testid='ask-user-input']").count() == 0
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0062 — Client-side JSON parse error renders inline before any /respond POST
# ===========================================================================


def test_u0062_invalid_json_blocks_respond_and_renders_inline_error(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0062 — For an object-schema prompt, the panel JSON.parse's
    the textarea content before POSTing. A parse error must render
    inline (data-testid='ask-user-error') WITHOUT issuing the POST.

    Defends against the panel falling through to send a malformed
    body that the server would 422 — better to fail fast client-side
    so the operator can fix the input without a server round-trip.
    """
    sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        schema = {"type": "object", "properties": {}}
        _route_pending(
            page, sid, prompt="Provide config", response_schema=schema,
        )
        # Track whether /respond was hit — if so, fail the test (the
        # client-side parse-error path is supposed to short-circuit
        # before any POST).
        respond_calls: list[dict] = []

        def _on_respond(route):
            respond_calls.append({"url": route.request.url})
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

        # Fill invalid JSON.
        page.locator("[data-testid='ask-user-textarea']").fill("{bad json")
        page.get_by_role("button", name="Send response").click()

        # Inline error appears.
        inline = page.locator("[data-testid='ask-user-error']")
        expect(inline).to_be_visible(timeout=3_000)
        # The error mentions valid JSON.
        text = (inline.text_content() or "").lower()
        assert "json" in text, f"expected JSON in inline error, got {text!r}"

        # Crucially, NO /respond POST was issued.
        # Small settle delay to be sure the click handler finished.
        page.wait_for_timeout(300)
        assert len(respond_calls) == 0, (
            f"client-side parse error should short-circuit, but "
            f"/respond was hit {len(respond_calls)} time(s)"
        )

        # Panel stays open.
        expect(
            page.locator("[data-testid='ask-user-panel']")
        ).to_be_visible()
    finally:
        _cleanup(base_url, cleanup_urls)
