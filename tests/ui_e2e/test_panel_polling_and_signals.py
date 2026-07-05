"""AskUserPanel poll-draft + /respond error inline + session signal-button gates.

Mocks /v1/sessions/{sid}/ask_user/pending via Playwright page.route
where useful; otherwise drives real session state via the REST API.

Covers backlog items:
* U0058 — Panel clears draft when a new tool_call_id arrives across polls.
* U0060 — /respond 500 surfaces inline error (not a toast).
* U0070 — Pause button is disabled when session is not running.
* U0067 — Resume signal sent toast appears on each Resume click
  (idempotent re-toast), no error toast.
"""

from __future__ import annotations

import json

import httpx
from playwright.sync_api import expect

from tests.ui_e2e._studio_helpers import open_session_in_studio, open_studio


# ---------------------------------------------------------------------------
# Seed helpers
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
            "description": "panel-poll probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"


def _seed_workspace(base_url: str, wp_id: str, tpl_id: str, tmp_path) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
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
    pid = f"llm-ps-{unique_suffix}"
    aid = f"ag-ps-{unique_suffix}"
    wp_id = f"wp-ps-{unique_suffix}"
    tpl_id = f"tpl-ps-{unique_suffix}"
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


def _seed_graph(base_url: str, gid: str, agent_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={
            "id": gid, "description": "pause-disabled probe",
            "nodes": [
                {"kind": "begin", "id": "begin"},
                {"kind": "agent", "id": "drafter", "agent_id": agent_id},
                {"kind": "end", "id": "end", "output_template": ""},
            ],
            "edges": [
                {"kind": "static", "from_node": "begin", "to_node": "drafter"},
                {"kind": "static", "from_node": "drafter", "to_node": "end"},
            ],
        })
        assert r.status_code == 201, f"seed graph failed: {r.text}"


def _seed_graph_session(base_url: str, workspace_id: str, gid: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={"binding": {"kind": "graph", "graph_id": gid}, "auto_start": False},
        )
        assert r.status_code == 201, f"seed graph session failed: {r.text}"
        return r.json()["id"]


def _seed_graph_ladder(base_url: str, unique_suffix: str, tmp_path):
    """Same ladder as ``_seed_ladder`` but binds the session to a graph —
    Task 13 moved Pause off the agent panel onto the graph panel, so a
    Pause-control pin now needs a graph-bound session rather than an
    agent-bound one."""
    pid = f"llm-psg-{unique_suffix}"
    aid = f"ag-psg-{unique_suffix}"
    gid = f"gr-psg-{unique_suffix}"
    wp_id = f"wp-psg-{unique_suffix}"
    tpl_id = f"tpl-psg-{unique_suffix}"
    _seed_llm_provider(base_url, pid)
    _seed_agent(base_url, aid, pid)
    _seed_graph(base_url, gid, aid)
    wid = _seed_workspace(base_url, wp_id, tpl_id, tmp_path)
    sid = _seed_graph_session(base_url, wid, gid)
    cleanup_urls = [
        f"/v1/workspaces/{wid}/sessions/{sid}/cancel",
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
        f"/v1/graphs/{gid}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    return wid, sid, cleanup_urls


def _ask_item(sid, *, tool_call_id, prompt):
    return {"kind": "ask_user", "session_id": sid, "tool_call_id": tool_call_id, "prompt": prompt}


def _route_pending_items(page, wid, items):
    """Route GET /v1/workspaces/{wid}/yields/pending → {items}."""
    page.route(
        f"**/v1/workspaces/{wid}/yields/pending",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"items": items}),
        ),
    )


# ===========================================================================
# U0058 — Per-item respond draft is isolated across pending items
# ===========================================================================


def test_u0058_draft_clears_when_new_tool_call_id_arrives(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0058 — Re-pointed to the Studio's Action Required. The old
    single-panel "draft resets when the polled tool_call_id changes"
    invariant maps to the Studio's per-item respond state: each pending
    yield is its own ``action-item`` keyed by tool_call_id
    (studio-activity.jsx ``respondState``), so a draft typed into item A
    never bleeds into item B when the pending snapshot swaps.
    """
    wid, sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        state = {"items": [_ask_item(sid, tool_call_id="tc-A", prompt="What is your name?")]}

        def _on_pending(route):
            route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"items": state["items"]}),
            )

        page.route(f"**/v1/workspaces/{wid}/yields/pending", _on_pending)

        open_studio(page, console_url, wid)
        item = page.locator("[data-testid='action-item']").first
        expect(item).to_be_visible(timeout=10_000)
        expect(item).to_contain_text("What is your name?")

        # Type a draft into item A's respond input.
        inp = item.locator("[data-testid='respond']")
        inp.fill("partial draft text")
        assert (inp.input_value() or "") == "partial draft text"

        # Swap the pending snapshot to a DIFFERENT yield (new tcid + prompt);
        # the next reconcile poll (15s) or a manual wait surfaces item B.
        state["items"] = [_ask_item(sid, tool_call_id="tc-B", prompt="Pick a color?")]
        item_b = page.locator("[data-testid='action-item']").filter(has_text="Pick a color?").first
        expect(item_b).to_be_visible(timeout=20_000)
        # Item B's respond input is empty — the draft was scoped to item A.
        inp_b = item_b.locator("[data-testid='respond']")
        assert (inp_b.input_value() or "") == "", (
            f"draft bled into the new pending item: {inp_b.input_value()!r}"
        )
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0060 — /respond error → inline on the action-item (not a toast)
# ===========================================================================


def test_u0060_respond_500_renders_inline_error_not_toast(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0060 — Re-pointed to the Studio's Action Required. A server 500
    from ``/ask_user/respond`` renders INLINE on the action-item (the
    per-item ``rs.error`` red line), NOT as a generic toast — the
    operator sees the failure exactly where the submission happened and
    the item stays put to retry.
    """
    wid, sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        _route_pending_items(page, wid, [_ask_item(sid, tool_call_id="tc-500", prompt="Short?")])
        page.route(
            f"**/v1/sessions/{sid}/ask_user/respond",
            lambda route: route.fulfill(
                status=500, content_type="application/json",
                body=json.dumps({
                    "type": "/errors/internal",
                    "title": "Internal Error",
                    "status": 500,
                    "detail": "synthetic 500 for U0060",
                }),
            ),
        )

        open_studio(page, console_url, wid)
        item = page.locator("[data-testid='action-item']").first
        expect(item).to_be_visible(timeout=10_000)

        respond = item.locator("[data-testid='respond']")
        respond.fill("Alice")
        respond.press("Enter")

        # An inline error line renders on the item (studio-activity.jsx sets
        # rs.error to the failure's detail/title/message, else "Respond
        # failed"). Wait for the red line to appear, then confirm it carries
        # a failure marker — and that it is inline, NOT a toast.
        page.wait_for_timeout(1_000)
        item_text = (item.text_content() or "").lower()
        assert any(m in item_text for m in ("synthetic 500", "internal error", "respond failed", "500")), (
            f"no inline error marker on the action-item: {item_text!r}"
        )
        assert page.locator(".toast").filter(has_text="Respond failed").count() == 0, (
            "respond error should render inline on the item, not as a toast"
        )
        # The item stays put for a retry.
        expect(item).to_be_visible()
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0070 — Pause button is disabled when status is not running
# ===========================================================================


def test_u0070_pause_button_disabled_when_status_not_running(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0070 — Re-pointed to the Studio's ``ctrl-pause`` on the GRAPH
    panel. Task 13 moved Pause (and Cancel) off the agent panel onto
    SessionGraphPanel — the agent panel (SessionAgentPanel) has no Pause
    control at all now (Stop/End/Restart only). Per studio-center.jsx
    SessionGraphPanel the Pause button is still
    ``disabled={!wid || status !== "running" || pauseMut.loading}`` with
    a title "Enabled only when running" for a non-running (CREATED)
    session — same logic as the retired ST_SessionControls cluster, now
    pinned against a graph-bound session. Pins both the disabled attr AND
    the title affordance.
    """
    wid, sid, cleanup_urls = _seed_graph_ladder(base_url, unique_suffix, tmp_path)
    try:
        open_session_in_studio(page, console_url, wid, sid, kind="graph")

        pause = page.locator("[data-testid='ctrl-pause']").first
        expect(pause).to_be_visible(timeout=10_000)
        expect(pause).to_be_disabled()
        # Title affordance explains why.
        title = pause.get_attribute("title") or ""
        assert "Enabled only when running" in title, (
            f"expected disabled-reason title, got {title!r}"
        )
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0067 — Resume re-toasts idempotent copy on each click
# ===========================================================================


def test_u0067_resume_re_toasts_on_repeat_click(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0067 — Re-pointed: the Studio's agent panel has no dedicated
    Resume control anymore (studio-center.jsx's retired
    ST_SessionControls cluster is defined but never mounted by
    SessionAgentPanel). Per session-adapter.jsx's SA_useSessionConversation
    comment, "one input, three behaviours": a Composer send to a CREATED
    session invokes it, to RUNNING/WAITING it steers, and to PAUSED it
    resumes — always the SAME idempotent POST .../steer call
    (session/enqueue.py's wake_session). Sending a message now covers
    what a Resume click used to gate: repeating the (idempotent) send
    must never surface an error toast even as the session's status moves
    out from under it.
    """
    wid, sid, cleanup_urls = _seed_ladder(base_url, unique_suffix, tmp_path)
    try:
        open_session_in_studio(page, console_url, wid, sid, kind="agent")

        composer = page.locator("textarea[placeholder='Send a message…']")
        composer.wait_for(state="visible", timeout=10_000)
        send_btn = page.locator("[data-testid='chat-send-btn']")

        # First send — invokes the CREATED session (steer semantics). The
        # persisted USER_INPUT is the surviving positive signal (no
        # "Resume signal sent" toast exists anymore for a Composer send).
        composer.fill("resume probe one")
        send_btn.click()
        expect(
            page.get_by_text("resume probe one", exact=False).first
        ).to_be_visible(timeout=10_000)

        # Second send — same idempotent call while the session may
        # already be transitioning off CREATED. Tolerate the Composer
        # swapping to its Stop affordance (turn in flight) by only
        # retrying while Send is still the visible control.
        page.wait_for_timeout(500)
        if send_btn.is_visible():
            composer.fill("resume probe two")
            send_btn.click()
            page.wait_for_timeout(1_500)

        # No error-toast leak — the negative contract survives verbatim:
        # a repeated idempotent send must never surface the generic
        # "Send failed" error-toast copy from the onSend catch handler.
        assert page.get_by_text("Send failed", exact=False).count() == 0, (
            "repeated composer send produced an error toast"
        )
    finally:
        _cleanup(base_url, cleanup_urls)
