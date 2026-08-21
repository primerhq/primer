"""UI E2E: an agent's open_file lands as a file tab in the attached client.

Smoke level by construction (crosscheck M16): S8 re-hosts this surface, so
this pins the CONTRACT (a client_action above the fence opens the file tab)
rather than the shell's chrome. It asserts tab PRESENCE, not focus:
presentation is host policy (crosscheck m11).

The delivery record is injected through the executor's own entry point
rather than by driving a real LLM turn, so the journey needs no model.

Spec section 7 also asks this journey to show "transcript renders the
pair". Without a model there is no real tool_call/tool_result pair to
render here, so that half is pinned one level down, on the mapping that
decides it: tests/ui/test_client_action_transcript.py (Task 9) runs the
real SA_toTranscript over a tool_call / client_action / tool_result
triple and asserts the pair survives while the delivery row is dropped.
"""

from __future__ import annotations

import os

import httpx
import pytest
from playwright.sync_api import expect

from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests.ui_e2e._studio_helpers import open_session_in_studio

# Collection gating lives in tests/ui_e2e/conftest.py, which sets
# ``collect_ignore_glob = ["test_*.py"]`` for the whole directory unless
# PRIMER_RUN_UI_E2E=1. A module-level collect_ignore_glob would be dead code
# (pytest only honours it in conftest), so the belt-and-braces guard here is
# the skipif below, matching tests/ui_e2e/test_graph_builder_feedback_loop.py.


def _seed(base_url: str, suffix: str) -> dict[str, str]:
    ids = {
        "llm": f"ct-llm-{suffix}",
        "wp": f"ct-wp-{suffix}",
        "tpl": f"ct-tpl-{suffix}",
        "agent": f"ct-ag-{suffix}",
        "workspace": "",
        "session": "",
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = seed_llm_provider_with(c, {
            "id": ids["llm"],
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed llm failed: {r.text}"
        r = c.post("/v1/workspace_providers", json={
            "id": ids["wp"],
            "provider": "local",
            "config": {"kind": "local", "root_path": f"/tmp/ct-{suffix}"},
        })
        assert r.status_code == 201, f"seed wp failed: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": ids["tpl"],
            "description": "client tools template",
            "provider_id": ids["wp"],
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed tpl failed: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        if r.status_code >= 500:
            pytest.skip(
                f"workspace create returned {r.status_code}; the primer-app "
                f"container likely cannot reach the provider path."
            )
        assert r.status_code == 201, f"seed workspace failed: {r.text}"
        ids["workspace"] = r.json()["id"]
        r = c.post("/v1/agents", json={
            "id": ids["agent"],
            "description": "client tools agent",
            "model": agent_model(ids["llm"], "fake-model"),
            "tools": [],
            "system_prompt": ["probe"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"
        r = c.post(
            f"/v1/workspaces/{ids['workspace']}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": ids["agent"]},
                "auto_start": False,
            },
        )
        assert r.status_code == 201, f"seed session failed: {r.text}"
        ids["session"] = r.json()["id"]
    return ids


def _cleanup(base_url: str, ids: dict[str, str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in (
            f"/v1/workspaces/{ids['workspace']}/sessions/{ids['session']}/cancel"
            if ids.get("session") else None,
            f"/v1/workspaces/{ids['workspace']}" if ids.get("workspace") else None,
            f"/v1/workspace_templates/{ids['tpl']}",
            f"/v1/workspace_providers/{ids['wp']}",
            f"/v1/agents/{ids['agent']}",
            f"/v1/llm_providers/{ids['llm']}",
        ):
            if url is None:
                continue
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


@pytest.mark.skipif(
    os.environ.get("PRIMER_RUN_UI_E2E") != "1",
    reason="Set PRIMER_RUN_UI_E2E=1 to run UI e2e tests",
)
def test_open_file_delivery_opens_the_file_tab(
    page, base_url: str, console_url: str, unique_suffix: str,
) -> None:
    ids = _seed(base_url, unique_suffix)
    wid, sid = ids["workspace"], ids["session"]
    file_name = f"ct-{unique_suffix}.txt"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        put = c.put(
            f"/v1/workspaces/{wid}/files?path={file_name}",
            json={"content": "hello\n", "encoding": "text"},
        )
        if put.status_code >= 500:
            _cleanup(base_url, ids)
            pytest.skip("workspace file PUT unreachable from the container")

    try:
        open_session_in_studio(page, console_url, wid, sid, kind="agent")

        # The adapter attached on mount, so the executor exists.
        page.wait_for_function(
            "() => !!window.__shellClientToolsExecutor", timeout=10_000
        )

        # A delivery record ABOVE the fence executes: assert the tab is
        # PRESENT (focus is host policy, not part of the contract).
        verdict = page.evaluate(
            """([sid, path]) => {
                const ex = window.__shellClientToolsExecutor;
                ex.setAttachment(sid, 0);
                return ex.handleEvent({
                  class: "client_action",
                  session_id: sid,
                  seq: 1000,
                  payload: {
                    call_id: "tc-1",
                    name: "client__open_file",
                    arguments: {path: path},
                  },
                });
            }""",
            [sid, file_name],
        )
        assert verdict == "executed"
        expect(
            page.locator('[data-testid^="shell-tab:"]').filter(has_text=file_name)
        ).to_have_count(1, timeout=10_000)

        # A replayed record (at or below the fence) renders only: no second
        # tab, no error.
        replayed = page.evaluate(
            """([sid, path]) => {
                const ex = window.__shellClientToolsExecutor;
                ex.setAttachment(sid, 5000);
                return ex.handleEvent({
                  class: "client_action",
                  session_id: sid,
                  seq: 1000,
                  payload: {
                    call_id: "tc-1",
                    name: "client__open_file",
                    arguments: {path: path},
                  },
                });
            }""",
            [sid, file_name],
        )
        assert replayed == "rendered"
        expect(
            page.locator('[data-testid^="shell-tab:"]').filter(has_text=file_name)
        ).to_have_count(1)
    finally:
        _cleanup(base_url, ids)
