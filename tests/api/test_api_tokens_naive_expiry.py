"""A naive ``expires_at`` is read as UTC, not a 500 (review of gaps-infra, GI-23).

``body.expires_at <= now`` compared a naive client value with an aware clock and raised ``TypeError``: ``"expires_at": "2099-01-01T00:00:00"`` answered
500. Triggers already read a time with no zone as UTC; this was the one client-supplied datetime that did not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio


async def test_a_naive_future_expires_at_is_read_as_utc(client):
    resp = await client.post("/v1/auth/tokens", json={"name": "naive", "scopes": ["mcp"], "expires_at": "2099-01-01T00:00:00"})

    assert resp.status_code == 201, resp.text
    assert datetime.fromisoformat(resp.json()["expires_at"]) == datetime(2099, 1, 1, tzinfo=timezone.utc)


async def test_a_naive_past_expires_at_is_refused_as_in_the_past(client):
    resp = await client.post("/v1/auth/tokens", json={"name": "old", "scopes": ["mcp"], "expires_at": "2001-01-01T00:00:00"})

    assert resp.status_code == 422 and "token_expires_in_past" in resp.text


async def test_a_naive_value_is_not_read_as_local_time(client):
    """A naive time a minute from now in UTC is in the future for every server timezone, and one a minute ago is the past."""
    soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    gone = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(tzinfo=None).isoformat()

    assert (await client.post("/v1/auth/tokens", json={"name": "soon", "scopes": [], "expires_at": soon})).status_code == 201
    assert (await client.post("/v1/auth/tokens", json={"name": "gone", "scopes": [], "expires_at": gone})).status_code == 422


async def test_an_aware_expires_at_and_none_behave_as_before(client):
    aware = await client.post("/v1/auth/tokens", json={"name": "aware", "scopes": [], "expires_at": "2099-01-01T00:00:00+02:00"})
    forever = await client.post("/v1/auth/tokens", json={"name": "forever", "scopes": []})

    assert aware.status_code == 201 and datetime.fromisoformat(aware.json()["expires_at"]) == datetime(2098, 12, 31, 22, tzinfo=timezone.utc)
    assert forever.status_code == 201 and forever.json()["expires_at"] is None
