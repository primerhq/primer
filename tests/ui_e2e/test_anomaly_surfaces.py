"""Anomaly-surface regression tests.

Each documented backend anomaly that the UI is supposed to surface has
its own test here. Setup creates the backend precondition via API
(httpx), then asserts the UI renders the documented surface.

Covers:
* U0008 - T0711 anomaly banner on toolset detail Tools tab when an
  MCP-HTTP toolset points at an unreachable URL (server returns a 5xx --
  an unreachable upstream is a NetworkError -> 504).

UI spec §5 documents this surface as required.
"""

from __future__ import annotations

import time

import httpx
import pytest

from tests.ui_e2e._studio_helpers import open_session_in_studio


from tests._support.smk import smk  # noqa: E402
from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests.ui_e2e._shell_helpers import open_legacy_route
pytestmark = smk("SMK-UI-02", "SMK-UI-05", status="partial")


# US-011b/US-014 triage (2026-08-29): the pre-existing ui_e2e overlay-mount
# race (orig handoff known-issue #1) - same family as
# test_mobile_modal_is_sheet.py's mobile params and
# test_agents_create.py's u0007. Failed once in the full US-014 E2E run,
# passed twice in a row in isolation with no code change; a real
# regression in this surface would still fail outright after the reruns.
@pytest.mark.flaky(reruns=2, reruns_delay=1)
def test_u0008_toolset_tools_tab_renders_t0711_anomaly_banner(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0008 - Create an MCP-HTTP toolset via API pointing at an
    unreachable URL. Opening its detail page's Tools tab must render
    the documented T0711 anomaly banner (not a generic Error nor a
    blank-page crash).

    Priority 3 - anomaly surface. The Tools tab calls
    GET /v1/toolsets/{id}/tools which returns a 5xx (an unreachable
    MCP-HTTP upstream classifies as NetworkError -> 504). The UI
    detects (tools.error.status >= 500 && config.transport === "http")
    and renders a dedicated Banner with retry + invalidate actions.
    """
    toolset_id = f"ts-u0008-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        # Create MCP-HTTP toolset pointing at a deliberately
        # unreachable URL - port 9999 on localhost is unlikely to
        # have anything listening.
        # allow_unreachable: this suite deliberately seeds an unreachable
        # MCP-HTTP toolset; opt out of the create-time connectivity probe.
        r = c.post("/v1/toolsets?allow_unreachable=true", json={
            "id": toolset_id,
            "provider": "mcp",
            "config": {
                "transport": "http",
                "config": {
                    "url": "http://127.0.0.1:9999/sse",
                    "headers": {},
                },
            },
        })
        assert r.status_code == 201, f"seed toolset failed: {r.text}"

    try:
        # Navigate to the toolset detail page; default tab loads, then
        # click Tools tab to trigger the /tools fetch.
        open_legacy_route(page, console_url, f"toolsets/{toolset_id}")
        page.locator("h1.page-title").get_by_text(toolset_id).first.wait_for(
            state="visible", timeout=10_000,
        )

        # Click the Tools tab. The detail page has Config / Tools /
        # Metadata tabs; "Tools" is the role-name target.
        page.get_by_role("tab", name="Tools").first.click()

        # The anomaly banner has title "Tools list unavailable" and
        # detail mentioning T0711. Wait for the banner - the fetch
        # has to actually hit the backend + 500 first.
        page.get_by_text("Tools list unavailable", exact=False).first.wait_for(
            state="visible", timeout=15_000,
        )

        # Defence: the documented T0711 reference must appear in the
        # banner detail (so a copy-edit that drops it gets caught).
        page_text = page.locator("body").inner_text()
        assert "T0711" in page_text, (
            "T0711 reference missing from the rendered anomaly banner - "
            f"copy drift?\n(body text - truncated for readability):\n"
            f"{page_text[:1500]}"
        )
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            try:
                c.delete(f"/v1/toolsets/{toolset_id}")
            except Exception:  # noqa: BLE001
                pass


def test_u0018_deep_link_reload_preserves_agent_detail_tools(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0018 - RETARGETED (uiv2 Wave 2): the tab-selection-survives-
    reload premise this test pinned no longer applies - the agent
    detail overlay is one direct-edit form now, not four routed tabs
    (AGENT_TABS/routerQuery.tab are gone; see test_u0033's sister
    retarget in test_routing_and_mutations.py for the Config-tab
    variant of this same change). What's still real and worth pinning:
    the ToolPicker (which superseded the old Tools tab) is actually
    populated with the agent's real tools both before AND after a
    reload - not just present, but showing the right data each time.
    """
    # Seed an LLM provider + agent so the detail page has data.
    provider_id = f"llm-u0018-{unique_suffix}"
    agent_id = f"ag-u0018-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": provider_id,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed LLM failed: {r.text}"
        r = c.post("/v1/agents", json={
            "id": agent_id,
            "description": "u0018 deep-link probe",
            "model": agent_model(provider_id, "fake-model"),
            # A real, always-available built-in tool (primer/toolset/
            # misc.py) so the ToolPicker's "selected · N" counter has
            # something non-zero to check before/after reload.
            "tools": ["misc__uuid_v4"],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"

    try:
        open_legacy_route(page, console_url, f"agents/{agent_id}")

        # Wait for the agent detail page to render.
        page.locator("h1.page-title").get_by_text(agent_id).first.wait_for(
            state="visible", timeout=10_000,
        )

        selected_chip = page.get_by_test_id("tool-picker-filter-selected")
        selected_chip.wait_for(state="visible", timeout=10_000)
        assert "1" in (selected_chip.inner_text() or ""), (
            f"expected the picker's selected-count chip to show 1 tool "
            f"before reload; got {selected_chip.inner_text()!r}"
        )

        page.reload(wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(agent_id).first.wait_for(
            state="visible", timeout=10_000,
        )

        # After reload, the ToolPicker must still be populated with the
        # SAME agent's real tools, not reset to empty or stuck loading.
        selected_chip_after = page.get_by_test_id("tool-picker-filter-selected")
        selected_chip_after.wait_for(state="visible", timeout=10_000)
        assert "1" in (selected_chip_after.inner_text() or ""), (
            f"expected the picker's selected-count chip to still show 1 "
            f"tool after reload; got {selected_chip_after.inner_text()!r}"
        )

        # Defence: the URL still addresses this agent after reload (no
        # ?tab= query in the new grammar - agents/{id} is a 2-segment
        # legacy route with an empty section slot, so the overlay target
        # is "agents::{id}", not "agents:tools:{id}").
        assert f"overlay=agents::{agent_id}" in page.url, (
            f"reload dropped the addressed agent: {page.url}"
        )
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            try:
                c.delete(f"/v1/agents/{agent_id}")
            except Exception:  # noqa: BLE001
                pass
            try:
                c.delete(f"/v1/llm_providers/{provider_id}")
            except Exception:  # noqa: BLE001
                pass


def test_u0013_session_detail_renders_t0399_stale_cache_notice(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    tmp_path,
) -> None:
    """U0013 - Opening a session in the Studio renders cleanly and does
    NOT surface the obsolete dev-only "Reads are authoritative" stale-
    cache banner (T0399 / T0555 / T0611).

    That info banner was intentionally removed in commit f4e56585
    ("drop stale spec annotations") because it referenced internal
    ticket ids end users have no context for; the removal is also
    pinned by tests/ui/test_stale_spec_annotations_removed.py. This
    e2e asserts the live rendered surface matches that decision: the
    session opens in the Studio (center agent panel mounts) and none of
    the removed banner copy appears.

    Re-pointed: the session-detail page is retired; the session opens as
    a Studio center tab (``panel-agent``) via the deep-link helper.

    Setup ladder mirrors test_t0042 (test_sessions_top_level.py:42-):
    LLM provider → agent → workspace provider → workspace template →
    workspace → session bound to the agent with auto_start=False so
    the worker pool doesn't attempt a real LLM call.
    """
    provider_id = f"llm-u0013-{unique_suffix}"
    agent_id = f"ag-u0013-{unique_suffix}"
    wp_id = f"wp-u0013-{unique_suffix}"
    tpl_id = f"wt-u0013-{unique_suffix}"
    workspace_id: str | None = None
    session_id: str | None = None
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        # 1. LLM provider - placeholder (no upstream call).
        r = seed_llm_provider_with(c, {
            "id": provider_id,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed LLM failed: {r.text}"

        # 2. Agent bound to the LLM provider.
        r = c.post("/v1/agents", json={
            "id": agent_id,
            "description": "u0013 session-detail probe",
            "model": agent_model(provider_id, "fake-model"),
            "tools": [],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"

        # 3. Workspace provider + template + workspace.
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201, f"seed wp failed: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id,
            "description": "u0013 template",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed template failed: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, f"seed workspace failed: {r.text}"
        workspace_id = r.json()["id"]

        # 4. Session bound to the agent, auto_start=False so the
        # worker pool doesn't try a real LLM call (placeholder key).
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
        # Open the session in the Studio — the center agent panel mounts,
        # confirming the session body rendered before asserting absences.
        open_session_in_studio(
            page, console_url, workspace_id, session_id, kind="agent",
        )

        # The removed dev-only banner must NOT appear. Its title copy and
        # the three internal ticket ids it carried are all gone.
        assert page.get_by_text("Reads are authoritative", exact=False).count() == 0, (
            "obsolete 'Reads are authoritative' stale-cache banner is back"
        )
        for ticket in ("T0399", "T0555", "T0611"):
            assert page.get_by_text(ticket, exact=False).count() == 0, (
                f"removed dev-only ticket reference {ticket} is back in the "
                "Studio session panel"
            )
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            for url in (
                f"/v1/sessions/{session_id}" if session_id else None,
                f"/v1/workspaces/{workspace_id}" if workspace_id else None,
                f"/v1/workspace_templates/{tpl_id}",
                f"/v1/workspace_providers/{wp_id}",
                f"/v1/agents/{agent_id}",
                f"/v1/llm_providers/{provider_id}",
            ):
                if url is None:
                    continue
                try:
                    c.delete(url)
                except Exception:  # noqa: BLE001
                    pass


def test_u0009_agent_tools_picker_isolates_one_unavailable_toolset(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0009 - RETARGETED (uiv2 Wave 2): an agent bound to TWO
    toolsets - one that loads cleanly and one whose catalogue entry
    comes back ``available: false`` (an unreachable MCP-HTTP server) -
    must still render the shared ToolPicker with the good toolset's
    tools selectable and the bad toolset called out separately. The
    page must NOT blank out; the failure must be confined to the one
    toolset, not the whole picker.

    The old per-toolset ``<ToolsetSection>``/T0711-banner mechanism
    this test originally pinned was deleted in Wave 2 (ToolPicker
    ruling, see test_t0711_banner_status.py): the backend now computes
    ``available``/``unavailable_reason`` per toolset at ``/tools``
    aggregation time (``primer/api/routers/providers.py``), and the
    picker surfaces an unavailable toolset as a small dashed pill
    (``<span class="mono">{id}</span> · unavailable``, the reason in
    its title tooltip) rather than a dedicated banner per panel.

    Good toolset: the built-in ``misc`` internal toolset (always
    available, returns 5 tools per primer/toolset/misc.py).
    Bad toolset: an MCP-HTTP toolset pointing at an unreachable
    URL - identical pattern to U0008's T0711 trigger.
    """
    provider_id = f"llm-u0009-{unique_suffix}"
    agent_id = f"ag-u0009-{unique_suffix}"
    bad_toolset_id = f"ts-u0009-bad-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        # Seed LLM (placeholder).
        r = seed_llm_provider_with(c, {
            "id": provider_id,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed LLM failed: {r.text}"

        # Seed the broken MCP-HTTP toolset (T0711 trigger).
        # allow_unreachable: this suite deliberately seeds an unreachable
        # MCP-HTTP toolset; opt out of the create-time connectivity probe.
        r = c.post("/v1/toolsets?allow_unreachable=true", json={
            "id": bad_toolset_id,
            "provider": "mcp",
            "config": {
                "transport": "http",
                "config": {
                    "url": "http://127.0.0.1:9999/sse",
                    "headers": {},
                },
            },
        })
        assert r.status_code == 201, f"seed bad toolset failed: {r.text}"

        # Seed agent registered with one tool from the good (misc)
        # toolset and one from the bad toolset. ``agent.tools`` holds
        # scoped ids (``<toolset_id>__<tool_name>``); the detail
        # Tools tab groups them by prefix and renders one panel per
        # source toolset.
        r = c.post("/v1/agents", json={
            "id": agent_id,
            "description": "u0009 per-toolset isolation probe",
            "model": agent_model(provider_id, "fake-model"),
            "tools": ["misc__uuid_v4", f"{bad_toolset_id}__placeholder_tool"],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"

    try:
        # Navigate directly to the agent detail overlay - no more
        # tab= deep-link, the Tools picker lives on the one form.
        open_legacy_route(page, console_url, f"agents/{agent_id}")
        page.locator("h1.page-title").get_by_text(agent_id).first.wait_for(
            state="visible", timeout=10_000,
        )

        # Bad toolset is called out via the picker's unavailable-
        # toolset summary - a dashed pill naming the toolset id next
        # to the word "unavailable" (unavailable_reason is a tooltip,
        # not asserted here). This is the mechanism that superseded
        # the old per-toolset T0711 banner. The summary isn't
        # paginated, so it's visible before any search/filter
        # interaction - check it first, before the search box below
        # would filter it out of the result set entirely.
        page.locator(
            f'span:has-text("{bad_toolset_id}"):has-text("unavailable")'
        ).first.wait_for(state="visible", timeout=15_000)

        # Good toolset renders its group header + at least one tool
        # row - confirms the picker's catalogue fetch resolved and
        # rendered through to TP_Row for the reachable toolset. The
        # catalogue-wide pager (6 tools/page) would bury "misc" many
        # pages behind the much larger builtin toolsets (the "system"
        # toolset alone carries 100+ tools) that sort ahead of it, so
        # scope down with the picker's own search box first - the same
        # thing an operator hunting for one toolset would do.
        page.get_by_test_id("tool-picker-filter").fill("misc")
        page.get_by_test_id("tool-picker-group-misc").first.wait_for(
            state="visible", timeout=15_000,
        )
        page.get_by_test_id("tool-picker-row-misc__uuid_v4").first.wait_for(
            state="visible", timeout=10_000,
        )

        # Defence: the page-title is still rendered (no blank crash).
        # The agent detail h1 carries the agent id - if a render error
        # blew up the whole ToolPicker, the title would still be
        # visible via the page chrome, but the picker wouldn't be. The
        # asserts above already prove the picker rendered through for
        # both toolsets; this is a final structural sanity check.
        assert page.locator("h1.page-title").first.is_visible(), (
            "agent detail title disappeared after the tools picker "
            "rendered - page may have blanked out instead of isolating "
            "the failure to the one toolset"
        )

        # Defence 2: the good toolset's tools are still selectable
        # (checkbox present and not disabled) alongside the bad
        # toolset's unavailable pill - proves the good panel wasn't
        # dragged down by the bad one.
        good_checkbox = page.locator(
            '[data-testid="tool-picker-row-misc__uuid_v4"] input[type="checkbox"]'
        )
        assert good_checkbox.is_enabled(), (
            "the reachable toolset's tool row should stay interactive "
            "even though a sibling toolset is unavailable"
        )
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            for url in (
                f"/v1/agents/{agent_id}",
                f"/v1/toolsets/{bad_toolset_id}",
                f"/v1/llm_providers/{provider_id}",
            ):
                try:
                    c.delete(url)
                except Exception:  # noqa: BLE001
                    pass
