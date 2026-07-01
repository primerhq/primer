"""UI tests for ask_user parks — re-pointed to the Studio's Action Required.

The Studio (PR-B) retired the session-detail ``AskUserPanel``. ask_user
parks now surface in the Studio's RIGHT sidebar ``action-required`` list
(``studio-activity.jsx`` → ``ActionRequired``): one ``action-item`` per
pending yield, driven by ``GET /v1/workspaces/{wid}/yields/pending``. An
``ask_user`` item renders a ``respond`` text input inside
``action-ask-controls`` (Enter-to-send → POST
``/sessions/{sid}/ask_user/respond``).

Strategy (unchanged in spirit): rather than drive a real agent through an
LLM until it yields, we ``page.route`` the workspace-scoped
``/yields/pending`` snapshot and the per-session ``/ask_user/respond``
mutation, then drive the Studio's Action Required controls. The session +
workspace rows are seeded via the REST API so the shell renders honestly.

Covered backlog items (re-pointed to the Studio):
* U0048 - ask_user park renders an action-item + respond control.
* U0049 - Submitting a response POSTs /respond and the item clears.
* U0051 - A server error on /respond renders inline (rs.error), not a toast.

U0050 (the old "Skip this prompt" → cancel-yielded-tool flow) has NO Studio
equivalent: the Action Required list only exposes a Cancel control for
watch/sleep yields (``cancel-yield``), never for ask_user, so there is no
skip affordance to pin. It is REMOVED — see the note where U0050 stood.
"""

from __future__ import annotations

import json

import httpx
from playwright.sync_api import expect

from tests.ui_e2e._studio_helpers import open_studio


# ---------------------------------------------------------------------------
# Seed helpers - shared with the rest of the UI suite's style.
# ---------------------------------------------------------------------------


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-07")


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
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


def _seed_ladder(base_url: str, unique_suffix: str, tmp_path):
    """Seed the four prerequisite rows + return (wid, sid, cleanup_urls)."""
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
    return wid, sid, cleanup_urls


# ---------------------------------------------------------------------------
# Route-mock helpers — the Studio's Action Required snapshot + respond
# ---------------------------------------------------------------------------


def _route_pending_items(page, wid: str, items: list[dict]):
    """Route GET /v1/workspaces/{wid}/yields/pending -> the given items.

    The ActionRequired resource reads ``data.items``; each item shape is
    ``{ kind, session_id, tool_call_id, prompt }`` (studio-activity.jsx).
    """
    page.route(
        f"**/v1/workspaces/{wid}/yields/pending",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"items": items}),
        ),
    )


def _ask_item(sid: str, *, tool_call_id: str = "tc-ui-1", prompt: str = "What is your name?") -> dict:
    return {
        "kind": "ask_user",
        "session_id": sid,
        "tool_call_id": tool_call_id,
        "prompt": prompt,
    }


# ---------------------------------------------------------------------------
# U0048 - ask_user park renders an action-item + respond control
# ---------------------------------------------------------------------------


def test_u0048_ask_user_panel_renders_when_pending_returns_200(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0048 - With the workspace ``/yields/pending`` snapshot carrying an
    ``ask_user`` item, the Studio's RIGHT sidebar Action Required list
    renders an ``action-item`` for it: the prompt text, the ``ask_user``
    kind, and a ``respond`` input inside ``action-ask-controls``. Pins the
    render contract from ``studio-activity.jsx`` ``ActionRequired``.
    """
    wid, sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        _route_pending_items(page, wid, [_ask_item(sid, prompt="What is your name?")])
        open_studio(page, console_url, wid)

        item = page.locator("[data-testid='action-item']").first
        expect(item).to_be_visible(timeout=10_000)
        expect(item).to_contain_text("What is your name?")
        expect(item).to_contain_text("ask_user")
        # The ask_user variant renders a respond text input (Enter to send).
        expect(item.locator("[data-testid='action-ask-controls']")).to_be_visible()
        expect(item.locator("[data-testid='respond']")).to_be_visible()
        # The count chip reflects the single pending action.
        expect(page.locator("[data-testid='action-required-count']")).to_contain_text("1")
    finally:
        _cleanup(base_url, cleanup_urls)


# ---------------------------------------------------------------------------
# U0049 - Submitting a response POSTs /respond and the item clears
# ---------------------------------------------------------------------------


def test_u0049_ask_user_panel_submit_collapses_and_toasts(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0049 - Type a response into the action-item's ``respond`` input,
    press Enter -> it POSTs ``/sessions/{sid}/ask_user/respond`` with the
    right body, and the item is optimistically removed from the list
    (ActionRequired ``hide()``); flipping the pending snapshot to empty
    keeps it gone on the reconcile refetch.

    (The Studio's respond handler removes the item optimistically rather
    than raising a toast, so the old "Response sent" toast assertion is
    dropped in favour of the item-clears contract.)
    """
    wid, sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        _route_pending_items(page, wid, [_ask_item(sid)])
        respond_calls: list[dict] = []

        def _on_respond(route):
            req = route.request
            respond_calls.append({"body": req.post_data, "method": req.method})
            route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps({"status": "accepted"}),
            )

        page.route(f"**/v1/sessions/{sid}/ask_user/respond", _on_respond)

        open_studio(page, console_url, wid)
        item = page.locator("[data-testid='action-item']").first
        expect(item).to_be_visible(timeout=10_000)

        # Fill the respond input + press Enter (the submit affordance).
        respond = item.locator("[data-testid='respond']")
        respond.fill("Alice")
        # Flip the snapshot to empty so the post-hide reconcile keeps it gone.
        page.unroute(f"**/v1/workspaces/{wid}/yields/pending")
        _route_pending_items(page, wid, [])
        respond.press("Enter")

        # The item is optimistically removed + the respond endpoint was hit.
        expect(page.locator("[data-testid='action-item']")).to_have_count(0, timeout=8_000)
        assert len(respond_calls) >= 1, "respond endpoint was not called"
        body = json.loads(respond_calls[-1]["body"] or "{}")
        assert body.get("tool_call_id") == "tc-ui-1", body
        assert body.get("response") == "Alice", body
    finally:
        _cleanup(base_url, cleanup_urls)


# ---------------------------------------------------------------------------
# U0050 - REMOVED (no Studio equivalent)
# ---------------------------------------------------------------------------
# The old "Skip this prompt" affordance (which POSTed the tool-agnostic
# cancel-yielded-tool endpoint and toasted "Skipped") lived on the retired
# session-detail AskUserPanel. The Studio's Action Required list only exposes
# a Cancel control (``cancel-yield``) for watch_files / sleep yields - NEVER
# for ask_user - so there is no skip surface to pin. Removed with this note
# rather than force-fitting a control the Studio does not render.


# ---------------------------------------------------------------------------
# U0051 - A server error on /respond renders inline (not a toast)
# ---------------------------------------------------------------------------


def test_u0051_ask_user_panel_renders_422_inline_for_schema_violation(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0051 - When ``/ask_user/respond`` returns a 422, the Studio's
    ActionRequired surfaces the failure INLINE on the action-item (the
    per-item ``rs.error`` red line), NOT as a generic toast, and the item
    stays put so the operator can retry.

    (The Studio has no client-side response_schema textarea/JSON-parse
    branch - a single ``respond`` input backs every ask_user park - so this
    now pins purely the server-error-renders-inline half of the old
    contract, which is the operator-facing invariant that survived.)
    """
    wid, sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        _route_pending_items(page, wid, [_ask_item(sid, prompt="Provide config")])
        # Server returns 422 for the submit.
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

        open_studio(page, console_url, wid)
        item = page.locator("[data-testid='action-item']").first
        expect(item).to_be_visible(timeout=10_000)

        respond = item.locator("[data-testid='respond']")
        respond.fill("something")
        respond.press("Enter")

        # Inline error text renders on the item; the friendly 422 summary the
        # API client builds surfaces (ui/foundation/api.js
        # ``_friendlyValidationDetail``). It is inline, not a toast.
        expect(item).to_contain_text("required fields are missing or invalid", timeout=5_000)
        assert page.locator(".toast").filter(has_text="required fields").count() == 0, (
            "422 should render inline on the action-item, not as a toast"
        )
        # The item stays put so the operator can retry.
        expect(item).to_be_visible()
    finally:
        _cleanup(base_url, cleanup_urls)
