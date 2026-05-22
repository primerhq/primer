"""Polling-cadence regression tests.

The matrix console keeps sidebar counts and the topbar worker pill in
sync with the API by polling — never via push. This module pins the
contract that those polled views catch up to the live state within
the documented interval, without requiring a manual refresh.

Covers:
* U0002 — Sessions sidebar count polls within ~6s of an API session
  create.
* U0003 — Topbar worker pool pill renders ``<active>/<total>``
  matching ``/v1/workers``.

Polling intervals (per ui/components/chrome.jsx):
* Sessions sub-counts (created+running+paused) — 5000 ms each.
* Workers — 5000 ms.
* Topbar /health — 2000 ms (drives the warn/err pill class).

We allow generous timeouts (12-15 s) so the first poll plus React's
batched render settles even on a cold start.
"""

from __future__ import annotations

import re
import time

import httpx
import pytest


def test_u0002_sessions_sidebar_count_polls_after_api_create(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    tmp_path,
) -> None:
    """U0002 — POST a session via the API to a fresh workspace and
    assert the sidebar Sessions counter increments to reflect the new
    row within one polling interval (≤12s budget, real cadence ≤5s).

    Priority 4 — polling cadence. The Sessions sidebar count is the
    sum of three sub-counts (CREATED + RUNNING + PAUSED) — each its
    own poll (chrome.jsx:94-102). The combined number renders only
    once all three have a value, so a fresh page needs one full
    interval to display a number at all.

    Setup ladder mirrors U0013 (anomaly): LLM provider → agent →
    workspace provider → template → workspace. Then we open the
    console, capture the baseline Sessions count, POST a session via
    API with auto_start=false (no real LLM call), and poll the
    sidebar until the count is baseline+1.
    """
    provider_id = f"llm-u0002-{unique_suffix}"
    agent_id = f"ag-u0002-{unique_suffix}"
    wp_id = f"wp-u0002-{unique_suffix}"
    tpl_id = f"wt-u0002-{unique_suffix}"
    workspace_id: str | None = None
    session_id: str | None = None
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
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
            "model": {
                "provider_id": provider_id,
                "model_name": "fake-model",
            },
            "tools": [],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "path": str(tmp_path)},
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
        # Open the dashboard (sidebar renders on any page).
        page.goto(f"{console_url}#/", wait_until="domcontentloaded")
        # The Sessions nav item carries a .count span when the three
        # status polls have all loaded. Find it via the label.
        sessions_nav = page.locator(
            ".nav-item:has(.label:text('Sessions'))"
        ).first
        sessions_nav.wait_for(state="visible", timeout=10_000)

        def _read_count() -> int | None:
            """Return the integer rendered in the Sessions .count, or
            None if the count hasn't loaded yet (no .count element
            present means the polls are still in flight)."""
            count_el = sessions_nav.locator(".count").first
            if count_el.count() == 0:
                return None
            txt = (count_el.text_content() or "").strip()
            try:
                return int(txt)
            except ValueError:
                return None

        # Wait until the baseline is rendered (first poll cycle).
        baseline: int | None = None
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            baseline = _read_count()
            if baseline is not None:
                break
            page.wait_for_timeout(250)
        assert baseline is not None, (
            "Sessions sidebar count never rendered within 12s — "
            "polls aren't loading at all on the freshly opened page"
        )

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

        # Wait for the sidebar to catch up. Real poll cadence is 5s
        # — budget 15s to absorb the worst-case overlap between
        # request firing and React batching.
        target = baseline + 1
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            now = _read_count()
            if now is not None and now >= target:
                break
            page.wait_for_timeout(250)
        final = _read_count()
        assert final is not None and final >= target, (
            f"Sessions sidebar count did not catch up to API state "
            f"within 15s: baseline={baseline} expected≥{target} "
            f"final={final}"
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


def test_u0003_topbar_worker_pill_renders_active_total_from_workers(
    page,
    base_url: str,
    console_url: str,
) -> None:
    """U0003 — The topbar worker pill renders ``<active>/<total>``
    consistent with ``GET /v1/workers``. The bringup runs
    ``matrix api --run-worker`` so the live container always has at
    least one worker; the pill text must include that worker's count.

    Priority 4 — polling cadence. The pill text comes from
    chrome.jsx:262 ``{activeWorkers}/{totalWorkers || "—"}`` where
    both numbers derive from the polled ``/v1/workers`` response.
    The poll fires every 5 s; we budget 12 s for first-render
    settling.

    The test does not write to the API — read-only against the
    container's preexisting worker. Cleanup is therefore not
    needed.
    """
    # Capture the live worker state up-front so we know what the
    # pill should display.
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.get("/v1/workers")
        assert r.status_code == 200, f"GET /v1/workers failed: {r.text}"
        body = r.json()
        items = body.get("items", [])
        api_total = len(items)
        api_active = sum(1 for w in items if w.get("status") == "active")
    # Sanity: bringup runs --run-worker; if this is zero, the
    # container is misconfigured and the loop should know.
    assert api_total >= 1, (
        f"GET /v1/workers returned 0 workers — UI cannot display a "
        f"pill that exercises this contract. Body: {body!r}"
    )

    page.goto(f"{console_url}#/", wait_until="domcontentloaded")

    # The pill carries class "worker-pill" (chrome.jsx:256). Locate
    # via class so we don't fight title/attribute drift.
    pill = page.locator(".worker-pill").first
    pill.wait_for(state="visible", timeout=10_000)

    # Wait for the pill to render a real "<active>/<total>" pair —
    # default while polling is "0/—" (totalWorkers is undefined
    # before the first response).
    expected_text = f"{api_active}/{api_total}"
    deadline = time.monotonic() + 12.0
    last_seen = ""
    while time.monotonic() < deadline:
        last_seen = (pill.text_content() or "").strip()
        if expected_text in last_seen:
            break
        page.wait_for_timeout(250)
    assert expected_text in last_seen, (
        f"Topbar worker pill never rendered {expected_text!r} "
        f"matching /v1/workers; last text was {last_seen!r}"
    )

    # Defence: the pill text matches the active/total integer
    # pattern (catches a regression where someone renders only one
    # of the two values).
    assert re.search(r"\d+/\d+", last_seen), (
        f"Topbar worker pill text {last_seen!r} doesn't match "
        f"the documented <active>/<total> pattern"
    )
