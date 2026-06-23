"""Graph editor Save-button gating, graph-bound session polling, and
the cross-page Create-agent-then-bind-to-session flow.

Covers:
* U0029 - Graph detail "Save" stays disabled until a node is added.
* U0004 - Session detail page reflects `ended` status without manual
  refresh after a graph-bound session terminates.
* U0041 - Create-agent-then-bind-to-session: the newly-created agent
  appears in the NewSessionModal binding selector.
"""

from __future__ import annotations

import re
import time

import httpx


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-04", status="partial")


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
            "model": {
                "provider_id": provider_id,
                "model_name": "fake-model",
            },
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
        assert r.status_code == 201, f"seed provider failed: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id,
            "description": "ui-e2e template",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed template failed: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, f"seed workspace failed: {r.text}"
        return r.json()["id"]


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# U0029 - Graph editor Save button disabled until a node is added
# ---------------------------------------------------------------------------


def test_u0029_graph_save_disabled_until_node_added(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0029 - Seed a graph via API (minimal agent→terminal skeleton).
    Open its detail page. The Save button must start disabled
    (``diffCount === 0`` per graphs.jsx:589). Click "Add node" →
    "Terminal" to introduce a structural change; Save must become
    enabled (``diffCount > 0``).

    Priority 1 - mutation feedback. Pins the Save-gating contract:
    Save reflects whether the in-editor graph differs from the
    loaded server state. Defends against a regression where Save
    is permanently enabled (over-eager save) or permanently
    disabled (broken diff detection).
    """
    provider_id = f"llm-u0029-{unique_suffix}"
    agent_id = f"ag-u0029-{unique_suffix}"
    graph_id = f"graph-u0029-{unique_suffix}"
    _seed_llm_provider(base_url, provider_id)
    _seed_agent(base_url, agent_id, provider_id)

    # Seed the graph via API directly so the test doesn't depend
    # on the create-modal flow (covered by U0028).
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={
            "id": graph_id,
            "description": "u0029 save-gating probe",
            "nodes": [
                {"kind": "begin", "id": "begin"},
                {"kind": "agent", "id": "start", "agent_id": agent_id},
                {"kind": "end", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "begin", "to_node": "start"},
                {"kind": "static", "from_node": "start", "to_node": "end"},
            ],
            "entry_node_id": "begin",
        })
        assert r.status_code == 201, f"seed graph failed: {r.text}"

    try:
        page.goto(
            f"{console_url}#/graphs/{graph_id}",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").get_by_text(
            graph_id, exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        # Wait for the editor toolbar to render (Save lives there).
        save_btn = page.get_by_role("button", name="Save", exact=True).first
        save_btn.wait_for(state="visible", timeout=10_000)

        # Initial state: Save is disabled (graph loaded but unchanged).
        assert save_btn.is_disabled(), (
            "Save button should start disabled on a freshly-loaded "
            "graph (diffCount === 0) - possible regression in "
            "GraphEditor diff detection"
        )

        # Add a node - click "Add node" then "Tool call" in the dropdown.
        # Tool call needs no agent picker and, added unconnected, yields
        # only a SOFT "no incoming edges" warning (not a hard topology
        # violation), so the diff dirties AND Save un-gates. (A second
        # End node would instead trip the hard "End not reachable from
        # Begin" rule, which legitimately keeps Save disabled.) Scope to
        # the dropdown item so we don't click a node drawn on the canvas.
        page.get_by_role(
            "button", name="Add node", exact=True,
        ).first.click()
        tool_item = page.locator(".dd-item", has_text="Tool call").first
        tool_item.wait_for(state="visible", timeout=5_000)
        tool_item.click()

        # Save must now be enabled (diffCount > 0). Allow a brief
        # window for React to re-render.
        deadline = time.monotonic() + 5.0
        enabled = False
        while time.monotonic() < deadline:
            if save_btn.is_enabled():
                enabled = True
                break
            page.wait_for_timeout(100)
        assert enabled, (
            "Save button did not become enabled after Add node → "
            "Tool call; diffCount did not register the structural "
            "change"
        )

        # Defence: the "unsaved changes" hint also appears.
        page.get_by_text(
            "unsaved changes", exact=False,
        ).first.wait_for(state="visible", timeout=5_000)
    finally:
        _cleanup(base_url, [
            f"/v1/graphs/{graph_id}",
            f"/v1/agents/{agent_id}",
            f"/v1/llm_providers/{provider_id}",
        ])


# ---------------------------------------------------------------------------
# U0004 - Graph-bound session ended status polls without manual refresh
# ---------------------------------------------------------------------------


def test_u0004_graph_bound_session_ended_status_polls_without_refresh(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    tmp_path,
) -> None:
    """U0004 - Seed a graph-bound session via API with auto_start=True
    so the worker picks it up immediately. The graph executor runs
    end-to-end in one turn (per commit 1bd07ec - _GraphTurnDriver
    emits graph_ended and the scheduler maps to ENDED). Open the
    session detail page; the live status caption polls every 2s
    while non-terminal and must reflect a terminal state
    (ended / failed / cancelled / completed) within 15s without a
    manual refresh.

    Priority 4 - polling cadence. Pins the session-detail.jsx:22
    polling contract on the graph dispatch path. The agent in the
    graph uses a placeholder LLM (no upstream), so the graph
    executor likely terminates via the fatal path (ConfigError on
    the agent's LLM build) - but it terminates cleanly with a
    terminal status, which is what the UI surfaces.
    """
    provider_id = f"llm-u0004-{unique_suffix}"
    agent_id = f"ag-u0004-{unique_suffix}"
    graph_id = f"graph-u0004-{unique_suffix}"
    wp_id = f"wp-u0004-{unique_suffix}"
    tpl_id = f"wt-u0004-{unique_suffix}"
    workspace_id: str | None = None
    session_id: str | None = None
    _seed_llm_provider(base_url, provider_id)
    _seed_agent(base_url, agent_id, provider_id)

    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        # Seed the graph.
        r = c.post("/v1/graphs", json={
            "id": graph_id,
            "description": "u0004 graph-bound probe",
            "nodes": [
                {"kind": "begin", "id": "begin"},
                {"kind": "agent", "id": "start", "agent_id": agent_id},
                {"kind": "end", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "begin", "to_node": "start"},
                {"kind": "static", "from_node": "start", "to_node": "end"},
            ],
            "entry_node_id": "begin",
        })
        assert r.status_code == 201, f"seed graph failed: {r.text}"

        # Seed workspace + graph-bound session (auto_start so the
        # worker picks it up).
        workspace_id = _seed_workspace(base_url, wp_id, tpl_id, tmp_path)
        r = c.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": graph_id},
                "auto_start": True,
            },
        )
        assert r.status_code == 201, f"seed session failed: {r.text}"
        session_id = r.json()["id"]

    try:
        page.goto(
            f"{console_url}#/sessions/{session_id}",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").get_by_text(
            session_id, exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        # Wait for the polled status to reflect a terminal state.
        # Real poll cadence is 2s (session-detail.jsx:22). Budget
        # 15s to absorb startup + a few poll cycles.
        terminal_words = ("ended", "cancelled", "failed", "completed")
        deadline = time.monotonic() + 15.0
        terminal_seen = False
        while time.monotonic() < deadline:
            body_text = (page.locator("body").text_content() or "").lower()
            if any(w in body_text for w in terminal_words):
                terminal_seen = True
                break
            page.wait_for_timeout(500)
        assert terminal_seen, (
            "graph-bound session detail never reflected a terminal "
            "status within 15s - polling stalled or status caption "
            "regression"
        )
    finally:
        cleanup = []
        if session_id:
            cleanup.append(f"/v1/sessions/{session_id}")
        if workspace_id:
            cleanup.append(f"/v1/workspaces/{workspace_id}")
        cleanup.extend([
            f"/v1/workspace_templates/{tpl_id}",
            f"/v1/workspace_providers/{wp_id}",
            f"/v1/graphs/{graph_id}",
            f"/v1/agents/{agent_id}",
            f"/v1/llm_providers/{provider_id}",
        ])
        _cleanup(base_url, cleanup)


# ---------------------------------------------------------------------------
# U0041 - Create-agent-then-bind-to-session cross-page flow
# ---------------------------------------------------------------------------


def test_u0041_create_agent_then_bind_to_session_flow(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    tmp_path,
) -> None:
    """U0041 - Open /agents, create a new agent via the modal (lands
    on /agents/{new-id}), then start an interactive session bound to it
    via the agent-detail "Chat" action. Assert:

    * the create-modal flow navigates to the new agent's detail page,
    * the agent-detail "Chat" action creates a chat and navigates to it,
    * the created chat is bound to the freshly-created agent (verified
      via the API).

    Priority 1 - cross-page mutation feedback. The new agent created in
    the modal is immediately usable for a bound conversation, without a
    manual page reload.
    """
    provider_id = f"llm-u0041-{unique_suffix}"
    agent_id = f"ag-u0041-{unique_suffix}"
    created_chat_id: str | None = None
    _seed_llm_provider(base_url, provider_id)

    try:
        # 1. Open Agents list and create a new agent via the modal.
        page.goto(f"{console_url}#/agents", wait_until="domcontentloaded")
        page.locator("h1.page-title").first.wait_for(
            state="visible", timeout=10_000,
        )

        page.get_by_role("button", name="New agent").first.click()
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=5_000)

        # Fill the form. Create stays disabled until a provider AND a
        # model are selected, so fill id + provider + model.
        modal.locator("input#na-id").fill(agent_id)
        modal.locator("select#na-llm-provider").select_option(
            value=provider_id,
        )
        modal.locator("select#na-model").select_option("fake-model")

        # Submit.
        modal.get_by_role("button", name="Create").first.click()

        # Wait for nav to the agent detail page.
        page.wait_for_url(
            re.compile(rf"#/agents/{re.escape(agent_id)}"),
            timeout=10_000,
        )
        page.locator("h1.page-title").get_by_text(
            agent_id, exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        # 2. Click "Chat" in the page header - POSTs /chats {agent_id}
        # and navigates to the chat detail route.
        page.get_by_role("button", name="Chat", exact=True).first.click()
        page.wait_for_url(re.compile(r"#/chats/.+"), timeout=15_000)

        # 3. Confirm via the API that the chat is bound to the new agent.
        created_chat_id = page.url.split("#/chats/")[-1].split("?")[0]
        assert created_chat_id, f"could not parse chat id from {page.url!r}"
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(f"/v1/chats/{created_chat_id}")
            assert r.status_code == 200, r.text
            assert r.json().get("agent_id") == agent_id, (
                f"chat {created_chat_id!r} not bound to new agent "
                f"{agent_id!r}: {r.text}"
            )
    finally:
        cleanup = []
        if created_chat_id:
            cleanup.append(f"/v1/chats/{created_chat_id}")
        cleanup.extend([
            f"/v1/agents/{agent_id}",
            f"/v1/llm_providers/{provider_id}",
        ])
        _cleanup(base_url, cleanup)
