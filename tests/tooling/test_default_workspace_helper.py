"""The journeys read the default workspace's id from the server instead of naming it (pivot P2 Task 3).

A journey used to open ``primer`` by name. The default workspace is whatever ``is_default`` says it is: ``primer`` on an install that
predates the column, ``prime`` on a fresh one. So the e2e and ui_e2e journeys ask the server (``GET /v1/workspaces``, one row has
``is_default`` true) through ``tests/_support/default_workspace.py``, and a static check keeps a journey from naming the id again.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from tests._support import default_workspace
from tests._support.default_workspace import (
    default_workspace_id,
    default_workspace_id_sync,
    pick_default_workspace_id,
)

TESTS = Path(__file__).resolve().parents[1]


# ---- picking the row ----------------------------------------------------------------------------------------------------------------


def test_picks_the_flagged_row():
    rows = [{"id": "a", "is_default": False}, {"id": "prime", "is_default": True}]
    assert pick_default_workspace_id(rows) == "prime"


def test_no_flagged_row_names_what_it_saw():
    with pytest.raises(AssertionError, match="a, b"):
        pick_default_workspace_id([{"id": "a"}, {"id": "b", "is_default": False}])


def test_an_empty_listing_is_named_as_such():
    with pytest.raises(AssertionError, match="no default workspace"):
        pick_default_workspace_id([])


def test_two_flagged_rows_name_both():
    rows = [{"id": "a", "is_default": True}, {"id": "b", "is_default": True}, {"id": "c", "is_default": False}]
    with pytest.raises(AssertionError, match="a, b, c") as raised:
        pick_default_workspace_id(rows)
    assert "2 rows" in str(raised.value)


def test_only_a_true_flag_counts():
    """A truthy string from a stale server must not pass for the boolean flag."""
    with pytest.raises(AssertionError):
        pick_default_workspace_id([{"id": "a", "is_default": "true"}])


# ---- reading it from a server -------------------------------------------------------------------------------------------------------


def _server(rows: list[dict], *, seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    """A transport that serves ``GET /v1/workspaces`` the way the API pages it (offset and limit, a ``total``)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        assert request.url.path == "/v1/workspaces", request.url
        limit = int(request.url.params["limit"])
        offset = int(request.url.params.get("offset", "0"))
        page = rows[offset : offset + limit]
        return httpx.Response(200, json={"offset": offset, "length": len(page), "total": len(rows), "items": page})

    return httpx.MockTransport(handler)


async def test_the_async_helper_reads_the_flagged_row():
    rows = [{"id": "ws-1", "is_default": False}, {"id": "prime", "is_default": True}]
    async with httpx.AsyncClient(transport=_server(rows), base_url="http://server") as client:
        assert await default_workspace_id(client) == "prime"


async def test_the_async_helper_pages_past_the_first_two_hundred_rows():
    """The shared e2e database accumulates workspaces; the default may sit beyond the first page."""
    rows = [{"id": f"ws-{i:04d}", "is_default": False} for i in range(450)]
    rows.append({"id": "prime", "is_default": True})
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_server(rows, seen=seen), base_url="http://server") as client:
        assert await default_workspace_id(client) == "prime"
    assert [r.url.params["offset"] for r in seen] == ["0", "200", "400"]


async def test_the_async_helper_raises_the_servers_refusal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "nope"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server") as client:
        with pytest.raises(httpx.HTTPStatusError):
            await default_workspace_id(client)


def test_the_sync_helper_reads_the_flagged_row_and_caches_it(monkeypatch):
    default_workspace_id_sync.cache_clear()
    seen: list[httpx.Request] = []
    rows = [{"id": "primer", "is_default": True}, {"id": "ws-1", "is_default": False}]
    real_client = httpx.Client

    def client(**kwargs):
        return real_client(transport=_server(rows, seen=seen), **kwargs)

    monkeypatch.setattr(default_workspace.httpx, "Client", client)
    try:
        assert default_workspace_id_sync("http://127.0.0.1:8765") == "primer"
        assert default_workspace_id_sync("http://127.0.0.1:8765") == "primer"
    finally:
        default_workspace_id_sync.cache_clear()
    assert len(seen) == 1, "the second call must come from the cache, not the server"
    assert str(seen[0].url).startswith("http://127.0.0.1:8765/v1/workspaces?")


def test_default_wid_strips_the_console_suffix(monkeypatch):
    """The ui_e2e helper takes the console URL a journey has and asks the server root."""
    pytest.importorskip("playwright.sync_api")
    from tests.ui_e2e import _shell_helpers

    asked: list[str] = []
    monkeypatch.setattr(_shell_helpers, "default_workspace_id_sync", lambda base_url: asked.append(base_url) or "prime")

    assert _shell_helpers.default_wid("http://127.0.0.1:8765/console/") == "prime"
    assert _shell_helpers.default_wid("http://127.0.0.1:8765/console") == "prime"
    assert _shell_helpers.default_wid("http://127.0.0.1:8765/") == "prime"
    assert asked == ["http://127.0.0.1:8765"] * 3


# ---- no journey names the legacy id -------------------------------------------------------------------------------------------------

# A journey that opens a workspace by the literal "primer", reaches it by REST path, or deep-links it. An id such as "primer-2" is a different workspace,
# and prose that merely mentions a link (a docstring quoting "``#/w/primer?doc=...``") is not one: the deep-link shape must follow a quote or an f-string brace.
_NAMES_THE_LEGACY_ID = re.compile(
    r"""open_\w+\(\s*\w+\s*,\s*\w+\s*,\s*["']primer["']"""  # open_view(page, console_url, "primer", ...)
    r"""|/v1/workspaces/primer(?![\w-])"""  # "/v1/workspaces/primer/sessions"
    r"""|(?<=[}"'])\#/w/primer(?![\w-])"""  # f"{console_url}#/w/primer"
)


def test_no_journey_names_the_legacy_id():
    journeys = [p for d in ("e2e", "ui_e2e") for p in sorted((TESTS / d).glob("test_*.py"))]
    assert len(journeys) > 100, "the scan found almost no journeys, so a clean result would prove nothing"
    offenders = [
        f"{p.parent.name}/{p.name}:{number}"
        for p in journeys
        for number, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if _NAMES_THE_LEGACY_ID.search(line)
    ]
    assert offenders == [], (
        "these journeys name the default workspace 'primer'; a fresh install creates 'prime'. Read the id with "
        "tests._support.default_workspace (default_workspace_id in e2e, default_wid in ui_e2e):\n  " + "\n  ".join(offenders)
    )


def test_the_scan_matches_the_shapes_it_is_meant_to_catch():
    """A pattern that stopped matching would pass every journey."""
    assert _NAMES_THE_LEGACY_ID.search('        open_view(page, console_url, "primer", "platform:agents")')
    assert _NAMES_THE_LEGACY_ID.search("open_shell(page, console_url, 'primer')")
    assert _NAMES_THE_LEGACY_ID.search('"/v1/workspaces/primer/sessions"')
    assert _NAMES_THE_LEGACY_ID.search('f"{base}#/w/primer"')
    assert _NAMES_THE_LEGACY_ID.search("page.goto('#/w/primer')")
    assert not _NAMES_THE_LEGACY_ID.search("``#/w/primer?doc=session:sess-carried-c019`` in the address bar")
    assert not _NAMES_THE_LEGACY_ID.search('open_view(page, console_url, wid, "platform:agents")')
    assert not _NAMES_THE_LEGACY_ID.search('"/v1/workspaces/primer-2/sessions"')
    assert not _NAMES_THE_LEGACY_ID.search('user="primer", password="primer"')
