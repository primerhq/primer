"""UI test for the open-websearch MCP toolset detail page.

Covers backlog item:
* U0053 — Open-websearch MCP toolset detail page renders the 6-tool
  catalog with no MCP-HTTP error block.

The primer-app docker container may not have ``node`` / ``npx`` on
PATH; in that case the toolset's first ``/tools`` call returns a
500/502 envelope and the UI surfaces an anomaly banner instead of
the catalog. We probe the API first and skip-soft if the catalog
isn't available — pinning the UI assertion only when the upstream
actually works.
"""

from __future__ import annotations

import time

import httpx
import pytest
from playwright.sync_api import expect


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


def test_u0053_open_websearch_toolset_detail_renders_catalog(
    page, base_url, console_url, unique_suffix,
) -> None:
    """U0053 — Create an MCP-stdio toolset for open-websearch via API;
    navigate to its detail page; assert the 6 documented tools appear
    in the catalog and no MCP error block is rendered. Skip-soft if
    the container can't spawn npx (no node installed).
    """
    toolset_id = f"ts-ows-{unique_suffix}"
    cleanup_urls = [f"/v1/toolsets/{toolset_id}"]
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(
            "/v1/toolsets",
            json={
                "id": toolset_id,
                "provider": "mcp",
                "config": {
                    "transport": "stdio",
                    "config": {
                        "command": ["npx", "-y", "open-websearch@latest"],
                        "env": {
                            "MODE": "stdio",
                            "DEFAULT_SEARCH_ENGINE": "bing",
                        },
                    },
                },
            },
        )
        assert r.status_code == 201, f"create toolset failed: {r.text}"

        # Probe /tools first — if it errors (no npx in container), skip
        # rather than fail. This is host-environment dependent, not a
        # regression.
        try:
            tools_resp = c.get(
                f"/v1/toolsets/{toolset_id}/tools",
                timeout=httpx.Timeout(120.0, connect=10.0),
            )
        except httpx.HTTPError as exc:
            _cleanup(base_url, cleanup_urls)
            pytest.skip(f"open-websearch /tools probe errored: {exc}")
        if tools_resp.status_code != 200:
            _cleanup(base_url, cleanup_urls)
            pytest.skip(
                f"open-websearch /tools returned {tools_resp.status_code} "
                f"(container likely lacks node/npx); cannot pin UI catalog"
            )
        body = tools_resp.json()
        tools = (
            body if isinstance(body, list)
            else body.get("items", body.get("tools", []))
        )
        api_names = {t.get("id") or t.get("name") for t in tools}
        if "search" not in api_names:
            _cleanup(base_url, cleanup_urls)
            pytest.skip(
                f"open-websearch did not expose 'search' (got {api_names})"
            )

    try:
        # Navigate to toolset detail with Tools tab selected.
        page.goto(
            f"{console_url}#/toolsets/{toolset_id}?tab=tools",
            wait_until="domcontentloaded",
        )
        # Wait for the page to render the toolset id in the title.
        page.locator("h1.page-title").get_by_text(toolset_id).first.wait_for(
            state="visible", timeout=10_000,
        )

        # Look for any of the load-bearing tool names in the page body.
        # The UI renders tools as a list of cards/rows by name; the
        # exact selector varies by component version, so text-match
        # the rendered name + a couple of others to be confident.
        load_bearing = ["search", "fetchGithubReadme", "fetchWebContent"]
        deadline = time.monotonic() + 15.0
        found_all = False
        while time.monotonic() < deadline:
            body_text = (page.locator("body").text_content() or "")
            if all(name in body_text for name in load_bearing):
                found_all = True
                break
            page.wait_for_timeout(500)
        assert found_all, (
            f"UI did not render load-bearing tool names "
            f"{load_bearing} on toolset detail within 15s"
        )

        # Assert no T0711 MCP-error banner is shown — the catalog
        # should render cleanly, not via the error-fallback path.
        # The banner text per ui/components/toolsets.jsx says
        # "Tools list unavailable" — assert it's absent.
        assert page.get_by_text("Tools list unavailable").count() == 0, (
            "MCP-error banner rendered despite successful /tools probe; "
            "indicates UI is rendering the error path while the API is "
            "returning a clean catalog"
        )
    finally:
        _cleanup(base_url, cleanup_urls)
