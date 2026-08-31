"""Polling-cadence regression tests.

The primer console keeps sidebar counts and the topbar worker pill in
sync with the API by polling — never via push. This module pins the
contract that those polled views catch up to the live state within
the documented interval, without requiring a manual refresh.

Covers:
* U0002 — Sessions sidebar count polls within ~6s of an API session
  create.
* U0003 — Topbar worker pool pill renders ``<active>/<total>``
  matching ``/v1/workers``.

Polling intervals (per ui/components/the console shell):
* Sessions sub-counts (created+running+paused) — 5000 ms each.
* Workers — 5000 ms.
* Topbar /health — 2000 ms (drives the warn/err pill class).

We allow generous timeouts (12-15 s) so the first poll plus React's
batched render settles even on a cold start.
"""

from __future__ import annotations

import time

import httpx
import pytest

from tests.ui_e2e._studio_helpers import open_studio, session_row, sessions_list
from tests._support.model_profiles import agent_model, seed_llm_provider_with


def test_u0002_sessions_sidebar_count_polls_after_api_create(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    tmp_path,
) -> None:
    """U0002 — Re-pointed to the Studio sidebar's per-workspace Sessions
    count. POST a session via the API to a fresh workspace and assert the
    Studio left-sidebar Sessions-section count increments to reflect the
    new row within one polling interval (≤15s budget, real cadence ~3s).

    Priority 4 — polling cadence. The old GLOBAL sidebar "Sessions" nav
    count was removed with the sessions list (the ``studio`` nav item
    carries no count). The Studio's SessionsSection instead polls
    ``/workspaces/{wid}/sessions`` every 3s and renders the row count in
    its ``sessions-header`` (the shell rail ``st-section-count``); a
    new API-created row surfaces there without a manual refresh.

    Setup ladder: LLM provider → agent → workspace provider → template →
    workspace. Open the Studio, capture the baseline session-row count,
    POST a session (auto_start=false), poll until a new row appears.
    """
    provider_id = f"llm-u0002-{unique_suffix}"
    agent_id = f"ag-u0002-{unique_suffix}"
    wp_id = f"wp-u0002-{unique_suffix}"
    tpl_id = f"wt-u0002-{unique_suffix}"
    workspace_id: str | None = None
    session_id: str | None = None
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
            "description": "u0002 polling probe",
            "model": agent_model(provider_id, "fake-model"),
            "tools": [],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201, f"seed wp failed: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id,
            "description": "u0002 template",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed template failed: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, f"seed workspace failed: {r.text}"
        workspace_id = r.json()["id"]

    try:
        # Open the Studio for the fresh workspace; the sidebar Sessions
        # section polls this workspace's sessions every 3s.
        open_studio(page, console_url, workspace_id)
        # RETARGET (uiv2 R2): the flat rail is now the workspace tree,
        # collapsed by default - expand once so child rows render at all
        # (they mount/unmount reactively as the session list polls from
        # then on, no re-click needed).
        page.get_by_test_id(f"nv-rail-ws:{workspace_id}").click()

        def _row_count() -> int:
            """Number of session rows currently rendered under this
            workspace's (expanded) tree row."""
            return page.locator(
                '[data-testid^="nv-rail-ws-session:"]'
            ).count()

        # Baseline: a brand-new workspace has zero session rows. Wait for
        # the Sessions section to have finished its first poll (the empty
        # "No sessions yet." copy is present).
        sessions_list(page).wait_for(
            state="visible", timeout=15_000,
        )
        baseline = _row_count()

        # POST the session via API to drive the increment.
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

        # Wait for the sidebar to catch up (3s poll) — budget 15s.
        # Wait for THIS session's row, not merely for the count to move.
        # An empty workspace makes the shell create a session of its own,
        # so any-increment was satisfied by that one and the wait exited
        # before the seeded row had arrived.
        target = baseline + 1
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if session_row(page, session_id, workspace_id).count() >= 1:
                break
            page.wait_for_timeout(250)
        assert session_row(page, session_id, workspace_id).count() >= 1, (
            f"seeded session row did not reach the rail within 15s: "
            f"baseline={baseline} sid={session_id}"
        )
        # Snapshot the count only AFTER confirming the target row is
        # present - reading it mid-loop raced session_row's own
        # expand-on-demand click (uiv2 R2: the tree starts collapsed),
        # so it could catch the DOM a tick before React flushed the
        # newly expanded children.
        final = _row_count()
        assert final >= target, (
            f"the rail's session-row count did not catch up to API state: "
            f"baseline={baseline} expected>={target} final={final}"
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
