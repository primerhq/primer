"""Navigation surfaces + signal-button feedback flows.

Covers:
* U0036 — Toolset detail Config tab deep-link survives reload.
* U0043 — Topbar worker-pill click navigates to Workers page.
* U0046 — Sessions list filter input narrows rows by id substring.
* U0030 — Session cancel button transitions row to ended/cancelled.
"""

from __future__ import annotations

import time

import httpx

from tests.ui_e2e._studio_helpers import open_session_in_studio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-01", status="partial")


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


def _seed_workspace(
    base_url: str, wp_id: str, tpl_id: str, tmp_path,
) -> str:
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
# U0036 — Toolset Config tab deep-link survives reload
# ---------------------------------------------------------------------------


def test_u0036_toolset_config_tab_deep_link_survives_reload(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0036 — Sister of U0018 (agent Tools) / U0033 (agent Config) /
    U0034 (agent Metadata) and U0045 (toolset Tools) for the toolset
    Config tab. Navigate to ``#/toolsets/<id>?tab=config``, confirm
    Config is selected, reload, confirm URL + aria-selected="true"
    on Config survive.

    Priority 6 — routing. Pins the toolset-detail tab-routing
    contract from toolsets.jsx:324
    (``["config","tools","sessions"].includes(routerQuery.tab) ?
    routerQuery.tab : "config"``). Config is the default — defends
    against a reload regression where the default-fallback path
    strips the query string.

    Uses an MCP-stdio toolset (placeholder command) for setup —
    no upstream call required, the tab routing happens before any
    /tools fetch is triggered.
    """
    toolset_id = f"ts-u0036-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/toolsets", json={
            "id": toolset_id,
            "provider": "mcp",
            "config": {
                "transport": "stdio",
                "config": {
                    # `command` is a list (argv[0]+args style) — first
                    # entry is the executable, remaining are args.
                    "command": ["echo", "placeholder"],
                    "args": [],
                    "env": {},
                },
            },
        })
        assert r.status_code == 201, f"seed toolset failed: {r.text}"

    try:
        page.goto(
            f"{console_url}#/toolsets/{toolset_id}?tab=config",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").get_by_text(
            toolset_id, exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        config_tab = page.get_by_role("tab", name="Config").first
        config_tab.wait_for(state="visible", timeout=5_000)
        assert config_tab.get_attribute("aria-selected") == "true", (
            f"Config tab not selected on initial deep-link nav; "
            f"aria-selected={config_tab.get_attribute('aria-selected')!r}"
        )

        page.reload(wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(
            toolset_id, exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        config_tab_after = page.get_by_role("tab", name="Config").first
        config_tab_after.wait_for(state="visible", timeout=5_000)
        assert config_tab_after.get_attribute("aria-selected") == "true", (
            f"Config tab lost selected state after reload; "
            f"aria-selected={config_tab_after.get_attribute('aria-selected')!r}"
        )
        assert "tab=config" in page.url, (
            f"reload dropped ?tab=config query: {page.url}"
        )
    finally:
        _cleanup(base_url, [f"/v1/toolsets/{toolset_id}"])


# U0043 (topbar worker-pill click navigates to /workers) pruned
# 2026-05-25 — narrow nav primitive whose surface is already exercised
# by U0073 (worker pill reflects drain signal — clicks through to the
# Workers page state) and U0099 (sidebar workers count matches API,
# rendered on the Workers page). The pill→/workers click handler is a
# 3-line affordance defended in passing by every test that lands on
# the Workers page. File kept so the history grep lands here.


# ---------------------------------------------------------------------------
# U0046 — Sessions list filter narrows rows
# ---------------------------------------------------------------------------


# U0046 REMOVED (no Studio equivalent) — this pinned the GLOBAL, cross-
# workspace ``#/sessions`` list's text-filter input narrowing rows by an
# id/agent/workspace substring (sessions-list.jsx). The Studio retired that
# global list; sessions now live in each workspace's Studio left-sidebar
# ``session-row`` list, which has NO text-filter input. There is no Studio
# surface for a cross-workspace substring filter, so the test is removed with
# this note (per the re-point guidance for sessions-LIST filter features).


# ---------------------------------------------------------------------------
# U0030 — Session cancel button transitions session to terminal
# ---------------------------------------------------------------------------


def test_u0030_session_cancel_button_transitions_row_to_terminal(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    tmp_path,
) -> None:
    """U0030 — Re-pointed to the Studio's ``ctrl-end``. This journey seeds
    an AGENT-bound session, which renders through ``SessionAgentPanel``
    (studio-center.jsx) — the interactive control set is End/Restart
    (``ctrl-end``/``ctrl-restart``); ``ctrl-cancel`` only exists on the
    GRAPH run view's ``SessionGraphPanel`` (autonomous sessions). Seed a
    CREATED agent session, open it in the Studio (agent panel), click the
    ``ctrl-end`` control and assert:

    * a "Session ended" toast appears (``SessionAgentPanel``'s End mutation
      ``onSuccess``),
    * the panel-header status transitions to a terminal value
      (ended / cancelled / failed) within a polling interval,
    * the ``ctrl-end`` control becomes disabled once terminal (per
      studio-center.jsx ``disabled={!wid || isEnded || endMut.loading}``).

    Note: the Studio's ctrl-end fires the cancel POST DIRECTLY (no
    confirmation modal — that surface was retired), so the old confirm
    step is dropped. ``ST_SessionPanel`` polls /sessions/{id} every 2s
    while non-terminal, so the status catches up without a manual refresh.
    """
    provider_id = f"llm-u0030-{unique_suffix}"
    agent_id = f"ag-u0030-{unique_suffix}"
    wp_id = f"wp-u0030-{unique_suffix}"
    tpl_id = f"wt-u0030-{unique_suffix}"
    workspace_id: str | None = None
    session_id: str | None = None
    _seed_llm_provider(base_url, provider_id)
    _seed_agent(base_url, agent_id, provider_id)
    workspace_id = _seed_workspace(base_url, wp_id, tpl_id, tmp_path)

    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": agent_id},
                "auto_start": False,
            },
        )
        assert r.status_code == 201, f"seed session failed: {r.text}"
        session_id = r.json()["id"]

    try:
        open_session_in_studio(
            page, console_url, workspace_id, session_id, kind="agent",
        )

        # The ctrl-end control fires the cancel POST directly.
        cancel_btn = page.locator("[data-testid='ctrl-end']").first
        cancel_btn.wait_for(state="visible", timeout=10_000)
        cancel_btn.click()

        # Toast appears.
        page.get_by_text("Session ended", exact=False).first.wait_for(
            state="visible", timeout=10_000,
        )

        # Status transitions to a terminal value within one polling
        # interval (2s) — budget 15s to absorb React batching + worker.
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
            "session status never transitioned to a terminal value "
            "within 15s after cancel"
        )

        # Defence: the ctrl-end control is now disabled (isEnded=true).
        cancel_btn_after = page.locator("[data-testid='ctrl-end']").first
        deadline = time.monotonic() + 5.0
        disabled = False
        while time.monotonic() < deadline:
            disabled = cancel_btn_after.is_disabled()
            if disabled:
                break
            page.wait_for_timeout(250)
        assert disabled, (
            "ctrl-end did not become disabled after session "
            "transitioned to terminal"
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
            f"/v1/agents/{agent_id}",
            f"/v1/llm_providers/{provider_id}",
        ])
        _cleanup(base_url, cleanup)
