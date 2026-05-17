"""E2E: ?worker_id filter on GET /v1/sessions.

Verifies that the worker_id query parameter is RECOGNISED by the list
endpoint. Uses a deliberately unused worker id; the endpoint must
return an empty items[] (filter takes effect) rather than the
unfiltered set (filter silently ignored — current behaviour without
the implementation).

Setup chain mirrors test_sessions_top_level.py: LLMProvider → Agent →
WorkspaceProvider → WorkspaceTemplate → Workspace → Session. The
session stays in CREATED status (no auto_start), so no actual worker
ever claims it — making the "absent worker_id returns empty" assertion
deterministic regardless of how many workers happen to be registered
during the test run.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest

from .test_sessions_top_level import (
    _create_workspace_and_session,
    _full_setup,
    _teardown_setup,
)


# ============================================================================
# T0733 — ?worker_id filter recognised and excludes unclaimed sessions
# ============================================================================


@pytest.mark.asyncio
async def test_t0733_sessions_filter_by_absent_worker_id_returns_empty(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0733 — GET /v1/sessions?worker_id=<absent> returns empty items[].

    BEFORE the filter is implemented the unfiltered result is returned
    and the just-created session leaks in; AFTER, the filter takes
    effect and items[] is empty.
    """
    env = await _full_setup(client, unique_suffix, tmp_path)
    workspace_id: str | None = None
    session_id: str | None = None
    try:
        workspace_id, session_id = await _create_workspace_and_session(
            client, tpl_id=env["tpl_id"], agent_id=env["agent_id"],
        )

        absent = f"wkr_{uuid.uuid4().hex}"
        resp = await client.get(
            "/v1/sessions",
            params={"worker_id": absent, "limit": 50, "offset": 0},
        )

        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        ids = [item["id"] for item in items]

        # The filter must take effect: our session must NOT appear.
        assert session_id not in ids, (
            f"?worker_id={absent!r} appears to be ignored — session "
            f"{session_id!r} leaked into the response: {ids!r}"
        )
        # And for a wholly-unused worker_id there should be zero matches.
        assert items == [], (
            f"unexpected items for unused worker_id={absent!r}: {items!r}"
        )
    finally:
        if workspace_id is not None:
            await client.delete(f"/v1/workspaces/{workspace_id}")
        await _teardown_setup(client, env)
