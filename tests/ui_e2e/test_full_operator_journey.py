"""UI E2E: multi-page operator console journey.

This is the first post-pivot UI user-journey test. One Playwright
function seeds the API with a realistic working set (providers,
workspace ladder, toolset, agent), then drives the operator console
through six sidebar pages and into two detail pages, asserting at
each step that the seeded entity is visible and the page renders
with no console errors / failed requests.

Per the iteration directive: at least 60% of new UI tests should be
multi-page user-journey tests. This is the first of that family.

Deliberately avoids any LLM-dispatch surface (no sessions, no graph
runs, no AskUserPanel) so the test runs anywhere — including without
LM Studio reachability.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest


# ---------------------------------------------------------------------------
# API seed helpers (reused style from existing UI tests)
# ---------------------------------------------------------------------------


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-02", "SMK-UI-03", "SMK-UI-04", "SMK-UI-06")


def _seed_full_set(base_url: str, suffix: str, ws_root: Path) -> dict[str, str]:
    """Seed one of each major entity via the API. Returns the ids."""
    ids = {
        "llm": f"j-llm-{suffix}",
        "emb": f"j-emb-{suffix}",
        "wp": f"j-wp-{suffix}",
        "tpl": f"j-tpl-{suffix}",
        "toolset": f"j-ts-{suffix}",
        "agent": f"j-ag-{suffix}",
        "graph": f"j-gr-{suffix}",
        "workspace": "",  # backend-assigned, filled below
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": ids["llm"],
            "provider": "openresponses",
            "models": [{"name": "stub-model", "context_length": 8192}],
            "config": {
                "url": "http://127.0.0.1:1",
                "api_key": "sk-not-used",
                "flavor": "other",
            },
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed llm failed: {r.text}"
        r = c.post("/v1/embedding_providers", json={
            "id": ids["emb"],
            "provider": "openai",
            "models": [{"name": "stub-embed"}],
            "config": {
                "url": "http://127.0.0.1:1",
                "api_key": "sk-not-used",
                "flavor": "other",
            },
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed emb failed: {r.text}"
        r = c.post("/v1/workspace_providers", json={
            "id": ids["wp"],
            "provider": "local",
            "config": {"kind": "local", "root_path": str(ws_root)},
        })
        assert r.status_code == 201, f"seed wp failed: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": ids["tpl"],
            "description": "journey template",
            "provider_id": ids["wp"],
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed tpl failed: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, f"seed workspace failed: {r.text}"
        ids["workspace"] = r.json()["id"]
        r = c.post("/v1/toolsets", json={
            "id": ids["toolset"],
            "provider": "mcp",
            "config": {
                "transport": "stdio",
                "config": {"command": ["echo"]},
            },
        })
        assert r.status_code == 201, f"seed toolset failed: {r.text}"
        r = c.post("/v1/agents", json={
            "id": ids["agent"],
            "description": "journey agent",
            "model": {"provider_id": ids["llm"], "model_name": "stub-model"},
            "tools": [],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"
        r = c.post("/v1/graphs", json={
            "id": ids["graph"],
            "description": "journey graph",
            "nodes": [{"kind": "agent", "id": "n1", "agent_id": ids["agent"]}],
            "edges": [],
            "entry_node_id": "n1",
        })
        assert r.status_code == 201, f"seed graph failed: {r.text}"
    return ids


def _cleanup(base_url: str, ids: dict[str, str]) -> None:
    """Best-effort cleanup; ignore individual failures so one stale
    row doesn't mask the rest of the unwind."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in (
            f"/v1/workspaces/{ids['workspace']}" if ids.get("workspace") else None,
            f"/v1/graphs/{ids['graph']}",
            f"/v1/agents/{ids['agent']}",
            f"/v1/toolsets/{ids['toolset']}",
            f"/v1/workspace_templates/{ids['tpl']}",
            f"/v1/workspace_providers/{ids['wp']}",
            f"/v1/embedding_providers/{ids['emb']}",
            f"/v1/llm_providers/{ids['llm']}",
        ):
            if url is None:
                continue
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_multi_page_operator_journey_no_llm(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    console_messages: list[dict],
    failed_requests: list[dict],
    tmp_path: Path,
) -> None:
    """Multi-page operator-console journey across 6 sidebar pages plus
    2 detail pages, asserting the seeded entities are visible and the
    console stays clean throughout.

    Pages traversed (in order):
      1. /             — dashboard (initial nav from conftest)
      2. /workspaces   — list, seeded workspace visible
      3. /workspaces/{id} — detail
      4. /agents       — list, seeded agent visible
      5. /agents/{id}  — detail
      6. /graphs       — list, seeded graph visible
      7. /toolsets     — list, seeded toolset visible
      8. /providers/llm — list, seeded provider visible
      9. /             — back to dashboard; sanity-check sidebar shape
    """
    ids = _seed_full_set(base_url, unique_suffix, tmp_path)
    try:
        # ----- 1. Dashboard (initial nav done by `page` fixture)
        page.locator("h1.page-title").first.wait_for(
            state="visible", timeout=10_000,
        )

        # ----- 2. Workspaces list
        page.goto(f"{console_url}#/workspaces", wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(
            "Workspaces", exact=False,
        ).first.wait_for(state="visible", timeout=10_000)
        # The workspace row uses the backend-assigned id; assert it's
        # in the rendered table.
        page.locator(f"tr:has-text('{ids['workspace']}')").first.wait_for(
            state="visible", timeout=10_000,
        )

        # ----- 3. Workspace detail (click the row)
        page.locator(f"tr:has-text('{ids['workspace']}')").first.click()
        # URL transitions to /workspaces/{id}
        page.wait_for_url(
            f"**/console/#/workspaces/{ids['workspace']}**", timeout=10_000,
        )
        # The detail page renders the workspace id somewhere in the
        # header — be permissive on layout, just confirm presence.
        page.get_by_text(ids["workspace"], exact=False).first.wait_for(
            state="visible", timeout=10_000,
        )

        # ----- 4. Agents list
        page.goto(f"{console_url}#/agents", wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(
            "Agents", exact=False,
        ).first.wait_for(state="visible", timeout=10_000)
        page.locator(f"tr:has-text('{ids['agent']}')").first.wait_for(
            state="visible", timeout=10_000,
        )

        # ----- 5. Agent detail
        page.locator(f"tr:has-text('{ids['agent']}')").first.click()
        page.wait_for_url(
            f"**/console/#/agents/{ids['agent']}**", timeout=10_000,
        )
        page.get_by_text(ids["agent"], exact=False).first.wait_for(
            state="visible", timeout=10_000,
        )

        # ----- 6. Graphs list
        page.goto(f"{console_url}#/graphs", wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(
            "Graphs", exact=False,
        ).first.wait_for(state="visible", timeout=10_000)
        page.locator(f"tr:has-text('{ids['graph']}')").first.wait_for(
            state="visible", timeout=10_000,
        )

        # ----- 7. Toolsets list
        page.goto(f"{console_url}#/toolsets", wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(
            "Toolsets", exact=False,
        ).first.wait_for(state="visible", timeout=10_000)
        page.locator(f"tr:has-text('{ids['toolset']}')").first.wait_for(
            state="visible", timeout=10_000,
        )

        # ----- 8. Providers > LLM list
        page.goto(
            f"{console_url}#/providers/llm", wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").first.wait_for(
            state="visible", timeout=10_000,
        )
        page.locator(f"tr:has-text('{ids['llm']}')").first.wait_for(
            state="visible", timeout=10_000,
        )

        # ----- 9. Back to dashboard
        page.goto(f"{console_url}#/", wait_until="domcontentloaded")
        page.locator("h1.page-title").first.wait_for(
            state="visible", timeout=10_000,
        )

        # ----- Console-error hygiene across the whole journey.
        # Filter out *network*-level errors (the browser logs every
        # 4xx/5xx response as a console "error" — that's the documented
        # anomaly surface, not a JS bug). What we're hunting here is
        # real JS errors (TypeError, ReferenceError, uncaught
        # promises, etc.) and CSP refusals, which always carry
        # additional descriptive text past the bare "Failed to load
        # resource" prefix.
        FAILED_RESOURCE_PREFIX = "Failed to load resource"
        errors = [
            m for m in console_messages
            if m["level"] in ("error", "pageerror")
            and not m["text"].startswith(FAILED_RESOURCE_PREFIX)
            and "favicon" not in m["text"].lower()
            and "ERR_ABORTED" not in m["text"]
        ]
        assert not errors, (
            f"Journey produced {len(errors)} unexpected console errors:\n"
            + "\n".join(f"  [{m['level']}] {m['text']}" for m in errors)
        )

        # ----- Failed-request hygiene. The /workspaces and /agents
        # polls may produce 404s briefly when a fresh row hasn't
        # propagated; allow 4xx but reject 5xx outright.
        server_5xx = [
            f for f in failed_requests
            if f.get("status") is not None and f["status"] >= 500
        ]
        assert not server_5xx, (
            f"Journey produced {len(server_5xx)} server-5xx network "
            f"failures:\n"
            + "\n".join(
                f"  [{f.get('status')}] {f.get('method')} {f.get('url')}"
                for f in server_5xx
            )
        )
    finally:
        _cleanup(base_url, ids)
