"""Journey: an open, idle session document asks the server for little (console review 2026-10-08, C-038).

The review's probe counted 82 requests in 45 s (about 110 a minute) from one open session that was doing nothing. With the live tap
connected and nothing in flight, the session row, the pending yields, the external-tool banner, the session list and the Files tree now
poll at 15 s instead of 2 to 5 s, and the workspace list is one poll instead of two. The window below is 30 s after the document settles:
the old behaviour makes about 55 requests in it, the new one about 25.
"""

from __future__ import annotations

import time

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_doc

_WINDOW_S = 30
_LIMIT = 40


@pytest.mark.timeout(150)
def test_an_idle_session_document_stays_quiet(page: Page, base_url: str, console_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        wid = c.get("/v1/workspaces").json()["items"][0]["id"]
        agent_id = c.get("/v1/agents", params={"limit": 1}).json()["items"][0]["id"]
        r = c.post(f"/v1/workspaces/{wid}/sessions", json={"binding": {"kind": "agent", "agent_id": agent_id}, "auto_start": False})
        assert r.status_code == 201, r.text
        sid = r.json()["id"]

    requests: list[tuple[float, str]] = []
    page.on("request", lambda req: requests.append((time.time(), req.url.split("?")[0])) if "/v1/" in req.url else None)
    open_doc(page, console_url, wid, "session", sid)
    expect(page.get_by_test_id(f"nv-session-doc:{sid}")).to_be_visible(timeout=20_000)
    page.wait_for_timeout(8_000)             # the first load and the tap's connect settle

    mark = len(requests)
    page.wait_for_timeout(_WINDOW_S * 1_000)
    window = requests[mark:]
    by_path: dict[str, int] = {}
    for _t, path in window:
        key = path.rsplit("/v1/", 1)[-1]
        by_path[key] = by_path.get(key, 0) + 1
    busiest = sorted(by_path.items(), key=lambda kv: -kv[1])[:6]
    assert len(window) <= _LIMIT, f"{len(window)} requests in {_WINDOW_S}s from one idle session document; busiest: {busiest}"
