"""Lifecycle transitions resolve open external tool calls."""

from __future__ import annotations

import pytest

from primer.model.external_tool import ExternalToolCall

# Reuse the steer suite's fixture stack (fake workspace backend + app)
# and its seeding helpers; pytest picks up the imported fixtures.
from tests.api.test_external_tools_steer import (  # noqa: F401
    _parked_over,
    _seed_agent,
    _seed_call,
    _seed_session,
    _setup_ws,
    app,
    client,
    pr,
    sp,
    wsr,
)


@pytest.mark.asyncio
async def test_session_cancel_flips_rows(client, wsr, sp):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_session(sp, wid, **_parked_over("sess-1"))
    await _seed_call(sp, "sess-1")
    r = await client.post(f"/v1/workspaces/{wid}/sessions/sess-1/cancel")
    assert r.status_code in (200, 202, 204), r.text
    row = await sp.get_storage(ExternalToolCall).get("etool-fixed-1")
    assert row.status == "cancelled"
    assert row.result["reason"] == "session cancelled"


@pytest.mark.asyncio
async def test_session_delete_flips_rows(client, wsr, sp):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_session(sp, wid, **_parked_over("sess-1"))
    await _seed_call(sp, "sess-1")
    r = await client.delete(
        f"/v1/workspaces/{wid}/sessions/sess-1", params={"force": "true"}
    )
    assert r.status_code == 204, r.text
    row = await sp.get_storage(ExternalToolCall).get("etool-fixed-1")
    assert row.status == "cancelled"


@pytest.mark.asyncio
async def test_yield_cancel_endpoint_flips_row(client, wsr, sp):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_session(sp, wid, **_parked_over("sess-1"))
    await _seed_call(sp, "sess-1")
    r = await client.post(
        "/v1/sessions/sess-1/yields/tc-1/cancel", json={"reason": "operator"}
    )
    assert r.status_code in (200, 202), r.text
    row = await sp.get_storage(ExternalToolCall).get("etool-fixed-1")
    assert row.status == "cancelled"
    assert row.result["reason"] == "operator"
