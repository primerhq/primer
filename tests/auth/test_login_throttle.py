"""Failed sign-ins are throttled per (username, client address) with an exponential backoff (architecture review A-07).

RUN on main: forty wrong passwords in a row for one account all answered 401 and the forty-first, correct one, answered 200, so the
login route was a free online guessing oracle. After five attempts in a row without a success, the next attempt for that username
from that address is refused with 429 ``too_many_attempts`` and a ``Retry-After`` header until the backoff has elapsed, WITHOUT
looking at the password (a correct one is refused too, and sets no cookie). The refusal does not depend on the account existing,
so it cannot be used to tell real usernames from invented ones. One success forgets the count. A different username, or the same
username from a different address, has its own count.

The state is in this process only (``tests/auth/test_login_throttle_unit.py`` pins the arithmetic with a fake clock).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from httpx import ASGITransport

# Re-export so pytest can resolve the fixtures used below.
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401

FREE_ATTEMPTS = 5


async def _register(client, name: str = "alice", password: str = "supersecret") -> None:
    resp = await client.post("/v1/auth/register", json={"username": name, "password": password})
    assert resp.status_code == 200, resp.text
    client.cookies.clear()


async def _login(client, name: str = "alice", password: str = "WRONG"):
    return await client.post("/v1/auth/login", json={"username": name, "password": password})


async def _fail_n_times(client, n: int, name: str = "alice") -> list[int]:
    return [(await _login(client, name)).status_code for _ in range(n)]


@pytest.mark.asyncio
async def test_the_attempts_after_five_failures_are_refused_with_a_retry_after(client):
    await _register(client)

    first = await _fail_n_times(client, FREE_ATTEMPTS)
    refused = await _login(client)

    assert first == [401] * FREE_ATTEMPTS
    assert refused.status_code == 429, refused.text
    assert refused.headers["content-type"].startswith("application/problem+json")
    wait = int(refused.headers["retry-after"])
    assert wait >= 1
    body = refused.json()
    assert body["extensions"]["error"] == "too_many_attempts"
    assert body["extensions"]["retry_after_seconds"] == wait, "the body and the header must agree"


@pytest.mark.asyncio
async def test_a_correct_password_is_refused_while_the_account_is_backing_off(client):
    await _register(client)
    await _fail_n_times(client, FREE_ATTEMPTS)

    resp = await _login(client, password="supersecret")

    assert resp.status_code == 429, "the password must not be examined while backing off"
    assert "primer_session" not in resp.cookies


@pytest.mark.asyncio
async def test_an_invented_username_is_throttled_exactly_like_a_real_one(client):
    """If only real accounts were throttled, a 429 would tell an attacker which usernames exist."""
    await _register(client)

    real = (await _fail_n_times(client, FREE_ATTEMPTS, "alice"), await _login(client, "alice"))
    invented = (await _fail_n_times(client, FREE_ATTEMPTS, "nobody"), await _login(client, "nobody"))

    assert real[0] == invented[0] == [401] * FREE_ATTEMPTS
    assert real[1].status_code == invented[1].status_code == 429
    assert real[1].json()["extensions"]["error"] == invented[1].json()["extensions"]["error"]


@pytest.mark.asyncio
async def test_another_username_is_not_affected(client):
    await _register(client)
    await _fail_n_times(client, FREE_ATTEMPTS, "alice")

    other = await _login(client, "bob")

    assert other.status_code == 401, "a failure count must not spill onto other usernames"


@pytest.mark.asyncio
async def test_the_same_username_from_another_address_is_not_affected(client, app):
    await _register(client)
    await _fail_n_times(client, FREE_ATTEMPTS)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.9", 4242)), base_url="http://test",
    ) as elsewhere:
        resp = await _login(elsewhere, password="supersecret")

    assert resp.status_code == 200, "the key is (username, address): another address has its own count"


@pytest.mark.asyncio
async def test_a_success_forgets_the_failures(client):
    await _register(client)
    first = await _fail_n_times(client, FREE_ATTEMPTS - 1)
    ok = await _login(client, password="supersecret")
    client.cookies.clear()
    again = await _fail_n_times(client, FREE_ATTEMPTS - 1)

    assert first == again == [401] * (FREE_ATTEMPTS - 1)
    assert ok.status_code == 200, "eight failures in total would be throttled without the reset"


@pytest.mark.asyncio
async def test_a_burst_cannot_get_past_the_limit_by_arriving_together(client):
    """The count is taken when the attempt STARTS: a password check takes long enough that twenty parallel guesses would all
    have passed a check made before it."""
    await _register(client)

    resps = await asyncio.gather(*[_login(client) for _ in range(20)])
    statuses = [r.status_code for r in resps]

    assert statuses.count(401) <= FREE_ATTEMPTS + 1, statuses
    assert statuses.count(429) >= 20 - FREE_ATTEMPTS - 1, statuses
