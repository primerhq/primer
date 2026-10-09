"""GET /v1/sessions/{id} when the session never wrote messages.jsonl (01a11fa8).

A session that ended before its first turn (turn_no 0, last_seq 0) has no messages.jsonl at all. The detail route's usage fold raised NotFoundError from the log read, and get_session_by_id logged it with a traceback on every view while serving usage: null. A missing log is an empty log: the fold now returns build_usage_frame([]) (the zero frame) and stays out of the ERROR path; any other read error still logs the ERROR and serves null.

Reuses the fake-workspace fixtures of test_session_usage_and_context_length.py (the same read_file seam, no file_info, so the stat key is None and nothing is cached).
"""
from __future__ import annotations

import logging

import httpx
import pytest

from tests.api.test_session_usage_and_context_length import (
    _FakeWorkspace, _seed_agent_and_profile, _seed_session,
)


class _BrokenReadWorkspace(_FakeWorkspace):
    """A workspace whose read_file fails with a real error, not a missing file."""

    async def read_file(self, path: str) -> bytes:
        raise RuntimeError(f"disk on fire: {path!r}")


def _usage_errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The detail route's "computing usage failed" ERROR records, if any."""
    return [
        record for record in caplog.records
        if record.levelno >= logging.ERROR
        and "computing usage failed" in record.getMessage()
    ]


@pytest.mark.asyncio
async def test_missing_log_serves_the_zero_frame_without_an_error_log(
    client: httpx.AsyncClient, app, fake_storage_provider, caplog,
):
    """A session that never ran a turn has no log: 200, the zero frame, no ERROR."""
    from primer.api.routers.tap import build_usage_frame

    await _seed_session(fake_storage_provider, "s-miss")
    await _seed_agent_and_profile(fake_storage_provider)
    ws = _FakeWorkspace()  # nothing written: messages.jsonl does not exist

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-miss")
    assert r.status_code == 200, r.text
    assert r.json()["usage"] == build_usage_frame([])
    assert _usage_errors(caplog) == []


@pytest.mark.asyncio
async def test_other_read_error_still_logs_the_error_and_serves_null(
    client: httpx.AsyncClient, app, fake_storage_provider, caplog,
):
    """A real read failure (not a missing file) keeps the ERROR log and usage null."""
    await _seed_session(fake_storage_provider, "s-broken")
    await _seed_agent_and_profile(fake_storage_provider)
    ws = _BrokenReadWorkspace()

    async def _get(wid):
        return ws if wid == "ws-1" else None
    app.state.workspace_registry.get_workspace = _get  # type: ignore[assignment]

    r = await client.get("/v1/sessions/s-broken")
    assert r.status_code == 200, r.text
    assert r.json()["usage"] is None
    assert len(_usage_errors(caplog)) == 1
