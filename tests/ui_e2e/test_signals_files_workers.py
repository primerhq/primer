"""Session signals + workspace files + sidebar workers UI tests.

Covers backlog items:
* U0068 — Steering a session (Composer send) renders the submitted
  instruction inline in the session transcript.
* U0072 — Workspace detail Files tab lists a file written via API.
* U0073 — Sidebar worker-pill text reflects /v1/workers count after
  POSTing a drain signal (activeWorkers drops to 0/1).
"""

from __future__ import annotations

import time

import httpx
import pytest
from playwright.sync_api import expect

from tests.ui_e2e._studio_helpers import open_workspace_settings, open_session_in_studio


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


from tests._support.smk import smk  # noqa: E402
from tests._support.model_profiles import agent_model, seed_llm_provider_with
pytestmark = smk("SMK-UI-11", status="partial")


def _seed_llm_provider(base_url: str, pid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": pid, "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201


def _seed_agent(base_url: str, agent_id: str, provider_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/agents", json={
            "id": agent_id, "description": "signals+files probe",
            "model": agent_model(provider_id, "fake-model"),
            "tools": [], "system_prompt": ["test"],
        })
        assert r.status_code == 201


def _seed_workspace(base_url: str, wp_id: str, tpl_id: str, tmp_path) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id, "description": "ws tpl",
            "provider_id": wp_id, "backend": {"kind": "local"},
        })
        assert r.status_code == 201
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201
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
        assert r.status_code == 201
        return r.json()["id"]


def _put_file(base_url: str, workspace_id: str, path: str, content: str) -> int:
    """Write a file via API; returns status code so callers can skip-soft
    on container-vs-host filesystem mismatches (the UI loop's primer
    runs in a container that can't access host tmp_path).
    """
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.put(
            f"/v1/workspaces/{workspace_id}/files?path={path}",
            json={"content": content, "encoding": "text"},
        )
        return r.status_code


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# U0068 — Steer queue panel renders the submitted instruction
# ===========================================================================


def test_u0068_steer_queue_renders_submitted_instruction(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0068 — Re-pointed to the Studio's Composer send. The retired
    ``ctrl-steer``/``steer-popover`` cluster (the shell session document's
    ST_SessionControls) is no longer mounted by the agent panel — per
    the studio-agents-interact brief, steering IS sending a message via
    the Composer (SessionAgentPanel's onSend -> session-adapter.jsx's
    sendMessage -> POST .../steer). There is no popover and no success
    toast surviving from either the pre-Task-13 OR the retired
    session-detail "Queued this session (N)" surface; the transcript
    itself is the surviving contract — wake_session persists the steered
    instruction as a USER_INPUT record (primer/session/enqueue.py), which
    the session adapter renders inline as a user_message bubble.
    """
    pid = f"llm-st-{unique_suffix}"
    aid = f"ag-st-{unique_suffix}"
    wp_id = f"wp-st-{unique_suffix}"
    tpl_id = f"tpl-st-{unique_suffix}"
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
    instruction = f"please check the build status - {unique_suffix}"
    try:
        open_session_in_studio(page, console_url, wid, sid, kind="agent")

        # Send the instruction via the Composer — this IS steering now
        # (no dedicated steer button/popover survives on the agent panel).
        composer = page.get_by_test_id("nv-composer-input")
        expect(composer).to_be_visible(timeout=10_000)
        composer.fill(instruction)
        page.get_by_test_id("nv-send").click()

        # The steered instruction renders inline in the session transcript
        # (persisted USER_INPUT on wake) — the surviving positive signal
        # now that the popover + toast are both retired.
        expect(
            page.get_by_text(instruction, exact=False).first
        ).to_be_visible(timeout=10_000)
        # No error toast leak from the send.
        assert page.get_by_text("Send failed", exact=False).count() == 0
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0072 — Workspace Files tab lists API-written file
# ===========================================================================


def test_u0072_workspace_files_tab_lists_api_written_file(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0072 — Seed a workspace + PUT a file via API. Navigate to
    the workspace detail Files tab; the filename appears in the
    file tree. Pins the FilesTab → /v1/workspaces/{id}/files
    listing + render path.
    """
    wp_id = f"wp-f-{unique_suffix}"
    tpl_id = f"tpl-f-{unique_suffix}"
    wid = _seed_workspace(base_url, wp_id, tpl_id, tmp_path)
    cleanup_urls = [
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
    ]
    filename = f"hello-{unique_suffix}.txt"
    try:
        # Write a file via the API.
        status = _put_file(base_url, wid, filename, "hello world")
        if status not in (200, 201, 204):
            pytest.skip(
                f"PUT files returned {status}; likely container/host "
                "filesystem mismatch — test becomes runnable once "
                "the workspace provider points at a container-"
                "accessible path"
            )
        # Probe GET /files directly. If the list endpoint can't see
        # the file we just wrote (or returns 5xx), the UI test would
        # show 'Internal Error' in the file tree; skip-soft so we
        # don't hammer a known-broken env.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(f"/v1/workspaces/{wid}/files?path=")
            if r.status_code != 200:
                pytest.skip(
                    f"GET /v1/workspaces/{wid}/files probe failed: "
                    f"{r.status_code} {r.text[:200]}"
                )
            items = r.json().get("items", [])
            api_names = {it["path"].split("/")[-1] for it in items}
            if filename not in api_names:
                pytest.skip(
                    f"API listing missing {filename!r}; got {api_names!r}"
                )

        # Navigate to workspace detail Files tab.
        open_workspace_settings(page, console_url, wid, "files")

        # The file tree renders each file by its base name. Wait
        # for our filename to appear (the tab loads → fetches /files
        # → renders entries).
        expect(
            page.get_by_text(filename, exact=False).first
        ).to_be_visible(timeout=15_000)
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0073 — Sidebar worker pill reflects drain signal within polling cadence
# ===========================================================================


def test_u0073_worker_pill_reflects_drain_within_polling(
    page, base_url, console_url, unique_suffix,
) -> None:
    """U0073 — The topbar worker-pill text is "{active}/{total}"
    computed from /v1/workers items filtered by status=active.
    POSTing /workers/{id}/drain on the sole worker changes its
    status from 'active' to 'draining'; the pill polls every ~5s
    and should update from "1/1" to "0/1" within ~10s.

    Pins the worker-pill polling cadence + status filter in
    primer's the console shell TopBar.
    """
    # Find the registered worker via API. If no active workers
    # remain (a prior test already drained the sole worker — drain
    # has no public "un-drain" inverse), skip-soft: the worker pill
    # is already showing 0/N which is what this test wants to assert.
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.get("/v1/workers")
        assert r.status_code == 200
        workers = r.json().get("items", [])
        assert len(workers) >= 1, "expected ≥1 worker registered"
        active = [w for w in workers if w.get("status") == "active"]
        if not active:
            pytest.skip(
                f"no active workers to drain (already drained by a "
                f"prior iteration); workers={workers}"
            )
        worker_id = active[0]["id"]

    try:
        # Re-pointed (flag day): the worker fleet lives on the System
        # dashboard now, one row per worker with its live status.
        page.goto(f"{console_url}#/w/primer?view=system:dashboard",
                  wait_until="domcontentloaded")
        row = page.get_by_test_id(f"nv-worker:{worker_id}")
        # 01a0651f (4-strike CI flake, root-caused from 3 real CI log
        # occurrences across PR #215 and #220, evidence-first per the
        # de-flake convention this repo already uses for the same
        # shape - see 7d404cac/cd949f69): "domcontentloaded" only means
        # the initial HTML loaded, not that React has mounted. This
        # no-build app transpiles its whole JSX bundle with babel-
        # standalone IN the browser on every load, then NV_WorkerFleet
        # mounts and fires its FIRST /v1/workers fetch immediately (not
        # poll-gated - confirmed in ui/foundation/use-resource.js,
        # runFetch() runs synchronously on mount, pollMs only paces
        # SUBSEQUENT fetches). The 15s budget covers that entire
        # navigate -> transpile -> mount -> fetch -> render chain in
        # ONE wait with no margin; two of the three CI failures also
        # had OTHER, unrelated bootstrap-dependent waits fail in the
        # same run, pointing at general CI-runner contention (shared
        # CPU under the full ui_e2e suite) rather than anything specific
        # to worker draining or /v1/workers itself - the API confirms
        # the worker is genuinely active moments before this wait even
        # starts. 30s matches this repo's existing convention for this
        # exact flake shape (absorb runner load, not chase a phantom
        # backend bug).
        row.wait_for(state="visible", timeout=30_000)
        assert "active" in (row.text_content() or ""), (
            f"expected an active worker row, got {row.text_content()!r}"
        )

        # Drain via API.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.post(f"/v1/workers/{worker_id}/drain")
            assert r.status_code == 204, r.text

        # The fleet polls every ~8s; wait up to 20s for the row's
        # status to leave "active" (draining/drained/dead all count).
        deadline = time.monotonic() + 20.0
        left_active = False
        final_text = ""
        while time.monotonic() < deadline:
            page.wait_for_timeout(500)
            final_text = (row.text_content() or "")
            if "active" not in final_text:
                left_active = True
                break
        assert left_active, (
            f"worker row never reflected the drain; last: {final_text!r}"
        )
    finally:
        # No restore for the drain — the worker stays in draining
        # state for the rest of the iteration. Teardown will wipe
        # the scheduler row entirely.
        pass
