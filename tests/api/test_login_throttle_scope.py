"""The sign-in throttle is not defeated by fresh usernames or IPv6 addresses, and change-password is throttled (review of gaps-security, GS-15, GS-40).

* An IPv6 client is keyed on its /64: a host rotating addresses inside one /64 shares ONE key per username (it used to get five free attempts per address).
* A per-address budget counts the FAILED logins of one address across every username (``LOGIN_ADDRESS_FREE_ATTEMPTS``): spraying one password at many
  usernames hits a 429 although each (username, address) key was fresh. A success gives its attempt back, so people behind one office address who sign in
  correctly never use the budget up.
* ``POST /v1/auth/change-password`` verifies ``current_password``: it runs through its own throttle keyed on the user id (a stolen session or token was an
  unthrottled password oracle).
Characterization: the per-(username, address) rule of ``tests/api/test_login_throttle.py`` is unchanged.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from primer.api.routers.auth import LOGIN_ADDRESS_FREE_ATTEMPTS
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401

pytestmark = pytest.mark.asyncio

FREE = 5


def _from(app, address: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app, client=(address, 4321)), base_url="http://test")


async def _register(client, name: str = "alice", password: str = "supersecret") -> None:
    assert (await client.post("/v1/auth/register", json={"username": name, "password": password})).status_code == 200
    client.cookies.clear()


async def _login(c, name: str, password: str = "WRONG"):
    return await c.post("/v1/auth/login", json={"username": name, "password": password})


# ---- IPv6 /64 -----------------------------------------------------------------------------------------------------------------------------------


async def test_addresses_inside_one_ipv6_64_share_the_throttle_key(client, app):
    await _register(client)
    codes = []
    for i in range(FREE + 1):
        async with _from(app, f"2001:db8:1:2::{i + 1:x}") as c:  # five different addresses, one /64
            codes.append((await _login(c, "alice")).status_code)

    assert codes == [401] * FREE + [429], codes


async def test_another_ipv6_64_has_its_own_count(client, app):
    await _register(client)
    for i in range(FREE):
        async with _from(app, f"2001:db8:1:2::{i + 1:x}") as c:
            await _login(c, "alice")

    async with _from(app, "2001:db8:1:3::1") as other:
        assert (await _login(other, "alice")).status_code == 401


async def test_ipv4_clients_are_still_keyed_on_the_whole_address(client, app):
    await _register(client)
    for _ in range(FREE):
        async with _from(app, "203.0.113.7") as c:
            await _login(c, "alice")

    async with _from(app, "203.0.113.7") as same, _from(app, "203.0.113.8") as neighbour:
        assert (await _login(same, "alice")).status_code == 429
        assert (await _login(neighbour, "alice")).status_code == 401


async def test_an_ipv4_mapped_ipv6_address_is_the_ipv4_address(client, app):
    await _register(client)
    for _ in range(FREE):
        async with _from(app, "::ffff:203.0.113.9") as c:
            await _login(c, "alice")

    async with _from(app, "203.0.113.9") as plain:
        assert (await _login(plain, "alice")).status_code == 429


# ---- the per-address budget across usernames ------------------------------------------------------------------------------------------------


async def test_failed_logins_across_usernames_hit_the_address_budget(client, app):
    await _register(client)
    codes = []
    async with _from(app, "198.51.100.20") as c:
        for i in range(LOGIN_ADDRESS_FREE_ATTEMPTS + 1):
            codes.append((await _login(c, f"user{i}")).status_code)  # every (username, address) key is fresh

    assert codes == [401] * LOGIN_ADDRESS_FREE_ATTEMPTS + [429], codes


async def test_the_address_refusal_is_the_same_429_shape_with_a_retry_after(client, app):
    await _register(client)
    async with _from(app, "198.51.100.21") as c:
        for i in range(LOGIN_ADDRESS_FREE_ATTEMPTS):
            await _login(c, f"user{i}")
        refused = await _login(c, "someone-new")

    assert refused.status_code == 429 and int(refused.headers["retry-after"]) >= 1
    assert refused.json()["extensions"]["error"] == "too_many_attempts"


async def test_successful_logins_do_not_spend_the_address_budget(client, app):
    await _register(client)
    async with _from(app, "198.51.100.22") as c:
        for _ in range(LOGIN_ADDRESS_FREE_ATTEMPTS * 3):
            assert (await _login(c, "alice", "supersecret")).status_code == 200  # an office behind one address

        assert (await _login(c, "nobody")).status_code == 401, "the budget is still untouched"


async def test_another_address_is_not_affected_by_a_spraying_one(client, app):
    await _register(client)
    async with _from(app, "198.51.100.23") as sprayer:
        for i in range(LOGIN_ADDRESS_FREE_ATTEMPTS + 1):
            await _login(sprayer, f"user{i}")
    async with _from(app, "198.51.100.24") as honest:
        assert (await _login(honest, "alice", "supersecret")).status_code == 200


async def test_a_username_refusal_does_not_double_count_against_the_address(client, app):
    """Hammering ONE locked username must not also burn the address budget (the refused attempt was never examined)."""
    await _register(client)
    async with _from(app, "198.51.100.25") as c:
        for _ in range(FREE):
            await _login(c, "alice")
        for _ in range(LOGIN_ADDRESS_FREE_ATTEMPTS * 2):
            assert (await _login(c, "alice")).status_code == 429

        assert (await _login(c, "bob")).status_code == 401, "bob's key and the address budget are still free"


# ---- change-password ------------------------------------------------------------------------------------------------------------------------


async def _signed_in(client) -> None:
    await _register(client)
    assert (await _login(client, "alice", "supersecret")).status_code == 200


async def test_change_password_with_a_wrong_current_password_is_throttled(client):
    await _signed_in(client)
    codes = []
    for _ in range(FREE + 1):
        resp = await client.post("/v1/auth/change-password", json={"current_password": "WRONG", "new_password": "anothersecret"})
        codes.append(resp.status_code)

    assert codes == [401] * FREE + [429], codes


async def test_a_throttled_change_password_does_not_look_at_the_password_and_carries_a_retry_after(client):
    await _signed_in(client)
    for _ in range(FREE):
        await client.post("/v1/auth/change-password", json={"current_password": "WRONG", "new_password": "anothersecret"})

    resp = await client.post("/v1/auth/change-password", json={"current_password": "supersecret", "new_password": "anothersecret"})

    assert resp.status_code == 429 and int(resp.headers["retry-after"]) >= 1, "the right password is refused too while backing off"
    assert resp.json()["extensions"]["error"] == "too_many_attempts"


async def test_a_correct_change_password_still_works_and_forgets_earlier_failures(client):
    await _signed_in(client)
    for _ in range(FREE - 2):
        await client.post("/v1/auth/change-password", json={"current_password": "WRONG", "new_password": "anothersecret"})

    ok = await client.post("/v1/auth/change-password", json={"current_password": "supersecret", "new_password": "anothersecret"})
    assert ok.status_code == 200, ok.text

    for _ in range(FREE):
        stale = await client.post("/v1/auth/change-password", json={"current_password": "WRONG", "new_password": "x" * 12})
        assert stale.status_code == 401, "the failures before the success no longer count"


async def test_the_change_password_throttle_is_keyed_on_the_user_not_the_address(client, app):
    await _signed_in(client)
    cookie = client.cookies

    for i in range(FREE):
        async with _from(app, f"192.0.2.{i + 10}") as c:  # a different address every time
            c.cookies = cookie
            assert (await c.post("/v1/auth/change-password", json={"current_password": "WRONG", "new_password": "anothersecret"})).status_code == 401

    async with _from(app, "192.0.2.99") as fresh:
        fresh.cookies = cookie
        resp = await fresh.post("/v1/auth/change-password", json={"current_password": "WRONG", "new_password": "anothersecret"})
    assert resp.status_code == 429
