"""E2E: the seed route keeps every built-in toolset's tool pages (pivot plan 01 Task 7).

The ensure pass's system-collection step used to regenerate with only the
``system`` and ``crud`` toolsets, so a ``POST /v1/setup/seed`` lost the pages of
the other built-in toolsets. This pins the round trip on a real server: seed,
then read a web tool page back. Lead-scheduled (e2e is CPU-exclusive and the
lead schedules it). Since slice 6 the page is ``reference/tools/web/web_search``
(Task 11 of this plan moved the tree to ``reference/``), and the e2e server
ships a one-page wiki (``scripts/e2e/bringup.sh`` sets ``PRIMER_WIKI_DIR``)
because a build with no hand-written pages writes no reference pages (lead
ruling (b)).
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_seed_keeps_every_toolsets_tool_pages(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/setup/seed")
    assert r.status_code == 200, r.text
    r = await client.get("/v1/collections/system/docs", params={"path": "reference/tools/web/web_search"})
    assert r.status_code == 200, r.text
    assert r.json().get("body"), r.text
