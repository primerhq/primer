"""The id of the default workspace, read from the server (pivot P2 Task 3).

The journeys used to open the workspace ``primer`` by name. The default workspace is whichever one the server says it is: ``primer`` on an
install that predates ``SystemState.default_workspace_id``, ``prime`` on a fresh one. ``GET /v1/workspaces`` marks it with a response-only
``is_default``, and these helpers read that mark, so a journey runs against either.

* ``default_workspace_id(client)``: for the e2e journeys, which hold an (authenticated) ``httpx.AsyncClient`` rooted at the server.
* ``default_workspace_id_sync(base_url)``: for the ui_e2e journeys, which hold only a URL (``tests/ui_e2e/_shell_helpers.py::default_wid``
  wraps it). Cached per URL, since a test session runs against one server.
* ``pick_default_workspace_id(rows)``: the pure pick, shared by both.

The listing is paged, not truncated: the e2e database is shared and long lived, so it can hold more workspaces than one page and the default
may sit beyond the first.
"""

from __future__ import annotations

import functools

import httpx

_PAGE = 200


def pick_default_workspace_id(rows: list[dict]) -> str:
    """The id of the one row whose ``is_default`` is true.

    Raises ``AssertionError`` naming every id it saw when no row, or more than one, is flagged: a journey that cannot tell which workspace is
    the default must say so, not guess the first.
    """
    flagged = [row["id"] for row in rows if row.get("is_default") is True]
    if len(flagged) == 1:
        return flagged[0]
    seen = ", ".join(str(row.get("id")) for row in rows) or "none"
    if not flagged:
        raise AssertionError(f"no default workspace: no row has is_default true; workspaces seen: {seen}")
    raise AssertionError(f"{len(flagged)} rows have is_default true ({', '.join(flagged)}); workspaces seen: {seen}")


def _done(body: dict, fetched: int) -> bool:
    """Whether a listing page was the last: short, empty, or the count reached the server's ``total``."""
    items = body["items"]
    total = body.get("total")
    return len(items) < _PAGE or (total is not None and fetched >= total)


async def default_workspace_id(client: httpx.AsyncClient) -> str:
    """The default workspace's id, from ``GET /v1/workspaces`` on a client rooted at the server."""
    rows: list[dict] = []
    while True:
        response = await client.get("/v1/workspaces", params={"limit": _PAGE, "offset": len(rows)})
        response.raise_for_status()
        body = response.json()
        rows.extend(body["items"])
        if _done(body, len(rows)):
            return pick_default_workspace_id(rows)


@functools.cache
def default_workspace_id_sync(base_url: str) -> str:
    """The default workspace's id, from ``GET {base_url}/v1/workspaces``. Cached per ``base_url``; a failure is not cached."""
    rows: list[dict] = []
    with httpx.Client(timeout=30) as client:
        while True:
            response = client.get(f"{base_url}/v1/workspaces", params={"limit": _PAGE, "offset": len(rows)})
            response.raise_for_status()
            body = response.json()
            rows.extend(body["items"])
            if _done(body, len(rows)):
                return pick_default_workspace_id(rows)
