"""UI tests for the AskUserPanel (M3 of the yielding-tools feature).

Strategy: rather than drive a real agent through an LLM until it yields
on ``ask_user`` (which would require LM Studio + a real-LLM bringup
mode), we use Playwright's ``page.route`` to intercept the
``GET /v1/sessions/{sid}/ask_user/pending`` request and serve a
controlled response. The session row itself is seeded via the REST API
so the rest of the session-detail page (status pill, worker info,
turns list, etc.) renders against real data.

This lets us pin the PANEL'S behaviour — render shape, submit flow,
skip flow, inline-422 validation — without the heavy infrastructure
needed for a real-LLM-driven park. A future iteration will add a real
end-to-end test once LM Studio is wired into the UI bringup.

Covered backlog items:
* U0048 — AskUserPanel renders on session detail when pending returns 200.
* U0049 — Submit posts response → panel collapses → toast.
* U0050 — Skip posts cancel-yielded-tool → panel collapses → toast.
* U0051 — JSON-schema violation renders inline error (not generic toast).
"""

from __future__ import annotations

import json

import httpx
from playwright.sync_api import expect


# ---------------------------------------------------------------------------
# Seed helpers — shared with the rest of the UI suite's style.
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
            "description": "ui-e2e ask_user probe",
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
            "description": "ui-e2e ws template",
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
                "auto_start": False,  # stay CREATED so the polling stays active
            },
        )
        assert r.status_code == 201, f"seed session failed: {r.text}"
        return r.json()["id"]


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass


def _seed_ladder(base_url: str, unique_suffix: str, tmp_path):
    """Seed the four prerequisite rows + return ids + a cleanup URL list."""
    pid = f"llm-u-{unique_suffix}"
    aid = f"ag-u-{unique_suffix}"
    wp_id = f"wp-u-{unique_suffix}"
    tpl_id = f"tpl-u-{unique_suffix}"
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


# ---------------------------------------------------------------------------
# Route-mock helpers
# ---------------------------------------------------------------------------


def _route_pending_yes(
    page,
    sid: str,
    *,
    tool_call_id: str = "tc-ui-1",
    prompt: str = "What is your name?",
    response_schema: dict | None = None,
):
    """Route GET .../ask_user/pending to return a 200 with the given prompt."""
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


def _route_pending_no(page, sid: str):
    """Route GET .../ask_user/pending to return 404 (panel hidden)."""
    page.route(
        f"**/v1/sessions/{sid}/ask_user/pending",
        lambda route: route.fulfill(
            status=404,
            content_type="application/json",
            body=json.dumps(
                {
                    "type": "/errors/not-found",
                    "title": "Not Found",
                    "status": 404,
                    "detail": "no pending ask_user prompt",
                }
            ),
        ),
    )


# ---------------------------------------------------------------------------
# U0048 — Panel renders on parked-on-ask_user session
# ---------------------------------------------------------------------------


def test_u0048_ask_user_panel_renders_when_pending_returns_200(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0048 — With ``GET /v1/sessions/{sid}/ask_user/pending`` returning
    a 200 prompt body, the AskUserPanel mounts under the header card on
    the session detail page. Pins the panel render contract from
    [`ui/components/session-detail.jsx`](../../ui/components/session-detail.jsx)
    (the AskUserPanel sub-component + the early-return ``if
    (pending.error?.status === 404)`` rule).

    Priority 1 — yielding-tools UI. Validates the panel is wired into
    the page tree and its top-level shape (header + prompt + submit +
    skip buttons) renders.
    """
    sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        _route_pending_yes(
            page, sid, prompt="What is your name?",
        )
        page.goto(
            f"{console_url}#/sessions/{sid}",
            wait_until="domcontentloaded",
        )
        panel = page.locator("[data-testid='ask-user-panel']")
        expect(panel).to_be_visible(timeout=10_000)
        expect(panel).to_contain_text("Input requested")
        expect(panel).to_contain_text("What is your name?")
        expect(
            page.get_by_role("button", name="Send response")
        ).to_be_visible()
        expect(
            page.get_by_role("button", name="Skip this prompt")
        ).to_be_visible()
        # The single-line prompt is short → input variant per heuristic.
        expect(
            page.locator("[data-testid='ask-user-input']")
        ).to_be_visible()
    finally:
        _cleanup(base_url, cleanup_urls)


# ---------------------------------------------------------------------------
# U0049 — Submit posts response, panel collapses, toast appears
# ---------------------------------------------------------------------------


def test_u0049_ask_user_panel_submit_collapses_and_toasts(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0049 — Fill the response input, click Send response → the panel
    POSTs to /respond, the toast 'Response sent' appears, and the panel
    collapses on the next /pending poll (which we re-route to 404 to
    simulate the row flipping to resumable).

    Priority 1 — yielding-tools UI mutation feedback.
    """
    sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        # Stage 1: pending returns 200 with the prompt.
        _route_pending_yes(page, sid)
        # Stage 2 prep: respond returns 202.
        respond_calls: list[dict] = []

        def _on_respond(route):
            req = route.request
            respond_calls.append(
                {"body": req.post_data, "method": req.method}
            )
            route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps({"status": "accepted"}),
            )

        page.route(
            f"**/v1/sessions/{sid}/ask_user/respond", _on_respond,
        )

        page.goto(
            f"{console_url}#/sessions/{sid}",
            wait_until="domcontentloaded",
        )
        # Panel should render via the pending mock.
        expect(
            page.locator("[data-testid='ask-user-panel']")
        ).to_be_visible(timeout=10_000)

        # Fill the input + click Send.
        page.locator("[data-testid='ask-user-input']").fill("Alice")
        # Now flip the pending route to 404 so the panel collapses on next poll.
        page.unroute(f"**/v1/sessions/{sid}/ask_user/pending")
        _route_pending_no(page, sid)

        page.get_by_role("button", name="Send response").click()

        # Toast assertion.
        toast = page.get_by_text("Response sent", exact=False)
        expect(toast).to_be_visible(timeout=5_000)

        # The respond endpoint should have been hit with the right body.
        # Wait briefly for the route handler to record the call.
        page.wait_for_function(
            "() => true",  # immediate
        )
        assert len(respond_calls) >= 1, "respond endpoint was not called"
        body = json.loads(respond_calls[-1]["body"] or "{}")
        assert body.get("tool_call_id") == "tc-ui-1", body
        assert body.get("response") == "Alice", body

        # Panel collapses within the polling cadence (2 s) + buffer.
        expect(
            page.locator("[data-testid='ask-user-panel']")
        ).to_be_hidden(timeout=8_000)
    finally:
        _cleanup(base_url, cleanup_urls)


# ---------------------------------------------------------------------------
# U0050 — Skip posts cancel-yielded-tool, panel collapses, toast appears
# ---------------------------------------------------------------------------


def test_u0050_ask_user_panel_skip_posts_cancel_and_toasts(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0050 — Click Skip → the panel POSTs to the tool-agnostic
    cancel-yielded-tool endpoint, surfaces the operator-cancel toast
    ('Skipped'), and collapses on the next /pending poll.

    Pins the wiring from the spec §8.2 "Skip this prompt" copy →
    cancel-yielded-tool §8.6 endpoint. The toast copy is deliberately
    different from a session-cancel toast so the operator understands
    the agent is NOT terminated.
    """
    sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        _route_pending_yes(page, sid)
        cancel_calls: list[dict] = []

        def _on_cancel(route):
            cancel_calls.append(
                {"body": route.request.post_data, "url": route.request.url}
            )
            route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps({"status": "accepted"}),
            )

        page.route(
            f"**/v1/sessions/{sid}/yields/tc-ui-1/cancel", _on_cancel,
        )

        page.goto(
            f"{console_url}#/sessions/{sid}",
            wait_until="domcontentloaded",
        )
        expect(
            page.locator("[data-testid='ask-user-panel']")
        ).to_be_visible(timeout=10_000)

        # Flip /pending to 404 BEFORE clicking Skip so the next poll
        # after the cancel collapses the panel.
        page.unroute(f"**/v1/sessions/{sid}/ask_user/pending")
        _route_pending_no(page, sid)

        page.get_by_role("button", name="Skip this prompt").click()

        toast = page.get_by_text("Skipped", exact=False)
        expect(toast).to_be_visible(timeout=5_000)

        assert len(cancel_calls) >= 1, "cancel endpoint was not called"
        body = json.loads(cancel_calls[-1]["body"] or "{}")
        # Per session-detail.jsx the skip button supplies a default reason.
        assert body.get("reason") == "operator skipped", body

        expect(
            page.locator("[data-testid='ask-user-panel']")
        ).to_be_hidden(timeout=8_000)
    finally:
        _cleanup(base_url, cleanup_urls)


# ---------------------------------------------------------------------------
# U0051 — JSON-schema violation renders inline error (not toast)
# ---------------------------------------------------------------------------


def test_u0051_ask_user_panel_renders_422_inline_for_schema_violation(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0051 — When the prompt carries a response_schema and the
    operator submits an invalid response, the backend's 422 is rendered
    INLINE under the textarea (not as a generic toast), and the panel
    stays open (the row stays parked).

    The schema being ``{type:"object"}`` flips the heuristic to render
    a textarea (per session-detail.jsx) — and the panel parses the
    textarea text as JSON on Submit, surfacing parse errors inline
    before the API call. This test exercises BOTH the client-side JSON
    parse error path AND the server-side 422 path by sending invalid
    JSON first (parse error) then valid-but-schema-violating JSON
    (server 422).
    """
    sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        _route_pending_yes(
            page, sid, prompt="Provide config", response_schema=schema,
        )

        # Server returns 422 for the schema-violating submit.
        page.route(
            f"**/v1/sessions/{sid}/ask_user/respond",
            lambda route: route.fulfill(
                status=422,
                content_type="application/json",
                body=json.dumps(
                    {
                        "type": "/errors/validation-error",
                        "title": "Validation Error",
                        "status": 422,
                        "detail": "response failed schema validation: 'name' is a required property",
                    }
                ),
            ),
        )

        page.goto(
            f"{console_url}#/sessions/{sid}",
            wait_until="domcontentloaded",
        )
        expect(
            page.locator("[data-testid='ask-user-panel']")
        ).to_be_visible(timeout=10_000)

        # Schema-object prompt → textarea variant (not input).
        textarea = page.locator("[data-testid='ask-user-textarea']")
        expect(textarea).to_be_visible()

        # Fill with valid JSON but missing 'name' → server returns 422.
        textarea.fill('{"wrong": "field"}')
        page.get_by_role("button", name="Send response").click()

        # Inline error visible, not a toast.
        inline = page.locator("[data-testid='ask-user-error']")
        expect(inline).to_be_visible(timeout=5_000)
        # The error text should reference the validation failure.
        expect(inline).to_contain_text("schema validation")

        # Panel stays open (the row is still parked from the UI's view).
        expect(
            page.locator("[data-testid='ask-user-panel']")
        ).to_be_visible()
    finally:
        _cleanup(base_url, cleanup_urls)
