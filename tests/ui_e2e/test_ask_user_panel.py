"""UI tests for ask_user parks — re-pointed to the Studio's Action Required.

The Studio (PR-B) retired the session-detail ``AskUserPanel``. ask_user
parks now surface in the Studio's RIGHT sidebar ``action-required`` list
(the shell rail's attention list): one decision card per
pending yield, driven by ``GET /v1/workspaces/{wid}/yields/pending``. An
``ask_user`` item renders a ``respond`` text input inside
``nv-ask-answer`` (Enter-to-send → POST
``/sessions/{sid}/ask_user/respond``).

Strategy (unchanged in spirit): rather than drive a real agent through an
LLM until it yields, we ``page.route`` the workspace-scoped
``/yields/pending`` snapshot and the per-session ``/ask_user/respond``
mutation, then drive the Studio's Action Required controls. The session +
workspace rows are seeded via the REST API so the shell renders honestly.

Covered backlog items (re-pointed to the Studio):
* U0048 - ask_user park renders a decision card + answer control.
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

from tests.ui_e2e._studio_helpers import (
    expand_debug_sidebar,
    open_session_in_studio,
    open_studio,
)


# ---------------------------------------------------------------------------
# Seed helpers - shared with the rest of the UI suite's style.
# ---------------------------------------------------------------------------


from tests._support.smk import smk  # noqa: E402
from tests._support.model_profiles import agent_model, seed_llm_provider_with
pytestmark = smk("SMK-UI-07")


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
            "description": "ui-e2e ask_user probe",
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


def _route_pending_items(page, wid: str, items: list[dict]) -> list[str]:
    """Route GET /v1/workspaces/{wid}/yields/pending -> the given items,
    plus the cross-workspace aggregate the rail's Inbox reads (uiv2 R2).

    Each item is the shape the per-workspace route sends:
    ``{session_id, kind, prompt, tool_call_id, parked_at}``.

    Returns the list the handler appends each intercepted URL to. A mock
    that quietly fails to match looks exactly like a surface rendering
    nothing, which is a slow and confusing thing to debug; callers assert
    on this so the two are told apart immediately.

    The trailing ``*`` matters: without it the glob stops matching the
    moment anything appends a query string.
    """
    seen: list[str] = []
    # Every request the page makes, so a miss can say what WAS asked for
    # rather than only that the pattern matched nothing.
    asked: list[str] = []
    page.on("request", lambda req: asked.append(req.url))
    _ASKED[id(page)] = asked

    def _handler(route):
        seen.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"items": items}),
        )

    page.route(f"**/v1/workspaces/{wid}/yields/pending*", _handler)
    # The console renders the cards INLINE in the session doc, whose
    # gates ride the session-scoped route; mock it with the same items.
    page.route(f"**/v1/workspaces/{wid}/sessions/*/yields/pending*", _handler)

    # uiv2 R2: the rail's Inbox badge now reads the cross-workspace
    # aggregate (GET /v1/yields/pending, no workspace in the path), not
    # the per-workspace route above. Mock it too, or the badge count
    # falls through to the REAL aggregate - which on a long-lived shared
    # stack answers with however much real attention residue has
    # accumulated, not "1". Its item shape needs workspace_id (the
    # per-workspace items above don't carry one, since the URL already
    # scopes them).
    def _aggregate_handler(route):
        seen.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "items": [
                    dict(it, workspace_id=it.get("workspace_id", wid))
                    for it in items
                ],
                "total": len(items),
            }),
        )

    page.route("**/v1/yields/pending*", _aggregate_handler)
    return seen


_ASKED: dict = {}


def _assert_polled(page, polled: list[str], *, timeout_ms: int = 10_000) -> None:
    """Wait until the shell has actually asked for the pending yields.

    The rail fetches on mount, so this normally returns on the first
    tick; it waits rather than asserting outright so a slow first paint
    does not read as a mock that never matched.
    """
    waited = 0
    while not polled and waited < timeout_ms:
        page.wait_for_timeout(250)
        waited += 250
    if not polled:
        asked = _ASKED.get(id(page), [])
        near = [u for u in asked if "yields" in u or "workspaces" in u]
        raise AssertionError(
            "the shell never requested the workspace's pending yields, so "
            "the mock matched nothing: everything below would be comparing "
            "an empty list against an empty list.\n"
            f"workspace-ish requests the page DID make ({len(asked)} total): "
            + "\n  ".join([""] + near[:25])
        )


def _ask_item(sid: str, *, tool_call_id: str = "tc-ui-1", prompt: str = "What is your name?") -> dict:
    return {
        "kind": "ask_user",
        "session_id": sid,
        "tool_call_id": tool_call_id,
        "prompt": prompt,
    }


# ---------------------------------------------------------------------------
# U0048 - ask_user park renders a decision card + answer control
# ---------------------------------------------------------------------------


def test_u0048_ask_user_panel_renders_when_pending_returns_200(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0048 - With the workspace ``/yields/pending`` snapshot carrying an
    ``ask_user`` item, the Studio's RIGHT sidebar Action Required list
    renders a decision card for it: the prompt text, the question
    kind, and a ``nv-ask-answer`` input. Pins the
    render contract from the shell rail's attention list.
    """
    wid, sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        polled = _route_pending_items(
            page, wid, [_ask_item(sid, prompt="What is your name?")],
        )
        # The console renders decision cards INLINE in the session doc
        # (the band row is the workspace-level affordance), so open the
        # session the mocked park belongs to.
        open_session_in_studio(page, console_url, wid, sid)

        # The rail has to have ASKED before anything it renders means
        # something.
        _assert_polled(page, polled)

        # One decision card per parked yield, keyed by tool_call_id. The
        # old Studio's action-item/respond/action-required-count testids
        # went with it; the shell renders this card in the rail and again
        # inline in the transcript, from one component.
        card = page.get_by_test_id("nv-ask:tc-ui-1")
        expect(card).to_be_visible(timeout=10_000)
        expect(card).to_contain_text("What is your name?")
        expect(card).to_have_attribute("data-kind", "question")
        expect(card.get_by_test_id("nv-ask-answer")).to_be_visible()
        # RETARGET (uiv2 R2): the attention band's count badge is now the
        # Inbox header's count, the cross-workspace attention feed.
        expect(
            page.get_by_test_id("nv-rail-inbox").locator(".nv-rail-count")
        ).to_contain_text("1")
    finally:
        _cleanup(base_url, cleanup_urls)


# ---------------------------------------------------------------------------
# U0049 - Submitting a response POSTs /respond and the item clears
# ---------------------------------------------------------------------------


def test_u0049_ask_user_panel_submit_collapses_and_toasts(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0049 - Type a response into the decision card's answer input,
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
        polled = _route_pending_items(page, wid, [_ask_item(sid)])
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

        # The console renders decision cards INLINE in the session doc
        # (the band row is the workspace-level affordance), so open the
        # session the mocked park belongs to.
        open_session_in_studio(page, console_url, wid, sid)
        _assert_polled(page, polled)
        card = page.get_by_test_id("nv-ask:tc-ui-1")
        expect(card).to_be_visible(timeout=10_000)

        # Fill the answer input + press Enter (the submit affordance).
        respond = card.get_by_test_id("nv-ask-answer")
        respond.fill("Alice")
        # Flip the snapshot to empty so the post-hide reconcile keeps it gone.
        page.unroute(f"**/v1/workspaces/{wid}/yields/pending")
        _route_pending_items(page, wid, [])
        respond.press("Enter")

        # The item is optimistically removed + the respond endpoint was hit.
        expect(page.get_by_test_id("nv-ask:tc-ui-1")).to_have_count(
            0, timeout=8_000,
        )
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
    The decision card surfaces the failure INLINE on the card (the
    per-item ``rs.error`` red line), NOT as a generic toast, and the item
    stays put so the operator can retry.

    (The Studio has no client-side response_schema textarea/JSON-parse
    branch - a single ``respond`` input backs every ask_user park - so this
    now pins purely the server-error-renders-inline half of the old
    contract, which is the operator-facing invariant that survived.)
    """
    wid, sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        polled = _route_pending_items(
            page, wid, [_ask_item(sid, prompt="Provide config")],
        )
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

        # The console renders decision cards INLINE in the session doc
        # (the band row is the workspace-level affordance), so open the
        # session the mocked park belongs to.
        open_session_in_studio(page, console_url, wid, sid)
        _assert_polled(page, polled)
        card = page.get_by_test_id("nv-ask:tc-ui-1")
        expect(card).to_be_visible(timeout=10_000)

        respond = card.get_by_test_id("nv-ask-answer")
        respond.fill("something")
        respond.press("Enter")

        # Inline error text renders on the item; the friendly 422 summary the
        # API client builds surfaces (ui/foundation/api.js
        # ``_friendlyValidationDetail``). It is inline, not a toast.
        expect(card).to_contain_text(
            "required fields are missing or invalid", timeout=5_000,
        )
        assert page.locator(".toast").filter(has_text="required fields").count() == 0, (
            "422 should render inline on the decision card, not as a toast"
        )
        # The card stays put so the operator can retry.
        expect(card).to_be_visible()
    finally:
        _cleanup(base_url, cleanup_urls)
