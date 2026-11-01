"""Password hashing is bounded: an anonymous login flood cannot starve the process (review of gaps-security, GS-15).

``login`` hashes the submitted password for an unknown, SSO-only or disabled account too (the timing equaliser), argon2id costs 64 MiB, and the throttle
keys on (username, address), so a client that varies the username was never refused: 200 logins ran 200 hashes at once on the shared default thread pool.
Now every hash and verify of the sign-in routes holds one of ``HASH_CONCURRENCY`` slots of a per-app gate; at most ``HASH_MAX_WAITING`` more wait for
one; any further request is refused at once with 503 ``busy`` and a ``Retry-After`` (nothing is hashed for it). The hash parameters are untouched, and a
login that is let through answers exactly what it did.
"""

from __future__ import annotations

import asyncio

import pytest

import primer.api.routers.auth as auth_router
from primer.auth.passwords import HASH_CONCURRENCY, HASH_MAX_WAITING
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _register(client, name: str = "alice", password: str = "supersecret") -> None:
    resp = await client.post("/v1/auth/register", json={"username": name, "password": password})
    assert resp.status_code == 200, resp.text
    client.cookies.clear()


def _counting_hash(monkeypatch, *, delay: float = 0.2):
    stats = {"in_flight": 0, "peak": 0, "hashes": 0}

    async def counting_hash(plaintext: str) -> str:  # stands in for the 64 MiB argon2 call
        stats["hashes"] += 1
        stats["in_flight"] += 1
        stats["peak"] = max(stats["peak"], stats["in_flight"])
        await asyncio.sleep(delay)
        stats["in_flight"] -= 1
        return "x"

    monkeypatch.setattr(auth_router, "hash_password", counting_hash)
    return stats


async def test_a_flood_of_distinct_usernames_runs_at_most_the_cap_at_once_and_the_overflow_answers_503(client, monkeypatch):
    await _register(client)
    stats = _counting_hash(monkeypatch)
    n = 200

    resps = await asyncio.gather(*[client.post("/v1/auth/login", json={"username": f"nobody{i}", "password": "guess"}) for i in range(n)])

    statuses = [r.status_code for r in resps]
    assert stats["peak"] <= HASH_CONCURRENCY, stats
    assert set(statuses) == {401, 503}, set(statuses)
    assert statuses.count(401) <= HASH_CONCURRENCY + HASH_MAX_WAITING, "only the slots and the bounded queue are served"
    assert statuses.count(401) == stats["hashes"], "a refused request hashes nothing"
    busy = next(r for r in resps if r.status_code == 503)
    assert busy.headers["retry-after"] == "1" and busy.json()["extensions"]["error"] == "busy"


async def test_a_flood_does_not_starve_the_default_thread_pool(client, monkeypatch):
    """Hashing runs on its own small executor: a to_thread call from anywhere else is not queued behind the hashes."""
    await _register(client)
    release = asyncio.Event()

    async def blocked_hash(plaintext: str) -> str:
        await release.wait()
        return "x"

    monkeypatch.setattr(auth_router, "hash_password", blocked_hash)
    flood = [asyncio.create_task(client.post("/v1/auth/login", json={"username": f"nobody{i}", "password": "g"})) for i in range(30)]
    await asyncio.sleep(0.1)

    assert await asyncio.wait_for(asyncio.to_thread(lambda: "unrelated work ran"), 2) == "unrelated work ran"
    release.set()
    await asyncio.gather(*flood)


async def test_an_unknown_username_still_costs_exactly_one_hash(client, monkeypatch):
    await _register(client)
    stats = _counting_hash(monkeypatch, delay=0)

    resp = await client.post("/v1/auth/login", json={"username": "nobody", "password": "guess"})

    assert resp.status_code == 401 and resp.json()["extensions"]["error"] == "invalid_credentials"
    assert stats["hashes"] == 1


async def test_login_success_and_wrong_password_answer_as_before(client):
    await _register(client)

    ok = await client.post("/v1/auth/login", json={"username": "alice", "password": "supersecret"})
    bad = await client.post("/v1/auth/login", json={"username": "alice", "password": "WRONG"})

    assert ok.status_code == 200 and "primer_session" in ok.cookies
    assert bad.status_code == 401 and bad.json()["extensions"]["error"] == "invalid_credentials"


async def test_the_gate_is_released_when_a_hash_fails(client, monkeypatch):
    await _register(client)

    async def boom(plaintext: str) -> str:
        raise RuntimeError("argon2 exploded")

    monkeypatch.setattr(auth_router, "hash_password", boom)
    for i in range(HASH_CONCURRENCY + HASH_MAX_WAITING + 5):
        try:
            await client.post("/v1/auth/login", json={"username": f"nobody{i}", "password": "g"})
        except RuntimeError:
            pass
    monkeypatch.undo()

    resp = await client.post("/v1/auth/login", json={"username": "alice", "password": "supersecret"})
    assert resp.status_code == 200, "a leaked slot would leave the gate shut"


# ---- the password length cap (4096): no stored password is longer --------------------------------------------------------------------------


async def test_a_login_password_over_the_cap_answers_422(client):
    await _register(client)

    resp = await client.post("/v1/auth/login", json={"username": "alice", "password": "x" * 4097})

    assert resp.status_code == 422


async def test_register_and_change_password_carry_the_same_cap(client):
    too_long = "x" * 4097
    assert (await client.post("/v1/auth/register", json={"username": "bob", "password": too_long})).status_code == 422
    await _register(client)
    await client.post("/v1/auth/login", json={"username": "alice", "password": "supersecret"})

    changed = await client.post("/v1/auth/change-password", json={"current_password": "supersecret", "new_password": too_long})
    stale = await client.post("/v1/auth/change-password", json={"current_password": too_long, "new_password": "anothersecret"})

    assert changed.status_code == 422 and stale.status_code == 422


async def test_a_password_at_the_cap_still_works(client):
    at_cap = "p" * 4096
    assert (await client.post("/v1/auth/register", json={"username": "alice", "password": at_cap})).status_code == 200
    client.cookies.clear()

    resp = await client.post("/v1/auth/login", json={"username": "alice", "password": at_cap})

    assert resp.status_code == 200
