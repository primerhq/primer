"""Review 4.8: the first-boot registration is atomic.

Before the fix the route checked "no user yet", then awaited the password
hash, then created, with no lock or compare-and-set, so two concurrent
first registrations (an attacker racing the operator) both passed the
check and both became admin. The fix hashes the password first, then
re-checks and creates under the provider's global lock (the same
primitive migrations take), so exactly one registrant wins and the loser
answers the existing registration-closed 409.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from primer.model.storage import OffsetPage
from primer.model.user import User

# Re-export so pytest can resolve the fixtures used below (the register
# route is anonymous: ``raw_client`` carries no session cookie).
# ``fake_storage_provider`` comes from the root tests/conftest.py.
from tests.api.conftest import (  # noqa: F401
    app,
    fake_provider_registry,
    raw_client as client,
)


async def _register(client, username: str, password: str = "supersecret"):
    return await client.post(
        "/v1/auth/register",
        json={"username": username, "password": password},
    )


@pytest.mark.asyncio
async def test_concurrent_first_registrations_produce_exactly_one_admin(
    client, fake_storage_provider,
):
    """Two concurrent first registrations: one 200, one 409, one admin row.

    The hashes run before the lock, so both requests reach the critical
    section; the lock (the fake provider serialises it like the real
    backends) is what keeps the second insert out.
    """
    results = await asyncio.gather(
        _register(client, "operator"),
        _register(client, "attacker"),
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], (
        f"the race produced the wrong outcomes: {codes} "
        f"({[r.text for r in results]!r})"
    )
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["extensions"]["error"] == "user_already_exists", loser.text

    storage = fake_storage_provider.get_storage(User)
    users = list((await storage.list(OffsetPage(offset=0, length=10))).items)
    assert len(users) == 1, (
        f"the race created {len(users)} user rows, want exactly one: {users!r}"
    )
    assert users[0].role == "admin"
    assert users[0].username in ("operator", "attacker")


@pytest.mark.asyncio
async def test_a_single_first_registration_still_works(
    client, fake_storage_provider,
):
    """The benign path: one first registration still creates the admin and
    sets the session cookie (and a second one still gets the 409)."""
    resp = await _register(client, "solo")
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == "solo"
    assert "primer_session" in resp.cookies

    again = await _register(client, "later")
    assert again.status_code == 409

    storage = fake_storage_provider.get_storage(User)
    users = list((await storage.list(OffsetPage(offset=0, length=10))).items)
    assert len(users) == 1
    assert users[0].username == "solo" and users[0].role == "admin"


# --- Fix round 1: pool-safe concurrent first registration ------------------
#
# A Postgres ``migration_lock`` waiter HOLDS a pooled connection while it
# polls, and the winner needs a SECOND connection for its insert, so the
# pre-fix code (every concurrent registration waits on the lock while
# holding a connection) exhausts a small pool and 500s the winner. The
# provider wrapper below models that pool with a two-connection budget:
# a connection need that cannot be met fails fast, the way the real 30 s
# pool timeout would fail it.


class _PoolExhausted(Exception):
    """The budgeted pool had no free connection when one was needed."""


class _BudgetedStorage:
    """The ``User`` storage behind the budget: the insert takes a connection."""

    def __init__(self, inner, pool: "_BudgetedProvider") -> None:
        self._inner = inner
        self._pool = pool

    async def create(self, item):
        await self._pool.acquire()
        try:
            return await self._inner.create(item)
        finally:
            self._pool.release()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _BudgetedProvider:
    """The fake provider seen through a small pool.

    A ``migration_lock`` waiter keeps a pooled connection for the whole
    wait (the real Postgres lock is polled on a connection it holds),
    and the winner's insert takes a second one. The pre-fix code let
    every concurrent registration become such a waiter, so a storm of
    them pins the budget while the lock is held (by the winner, or by
    another process), and the insert's connection need times out.
    """

    def __init__(self, inner, budget: int = 2, fail_after_s: float = 0.1) -> None:
        self._inner = inner
        self._sem = asyncio.Semaphore(budget)
        self._fail_after_s = fail_after_s

    async def acquire(self) -> None:
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self._fail_after_s)
        except TimeoutError:
            raise _PoolExhausted from None

    def release(self) -> None:
        self._sem.release()

    def get_storage(self, model):
        return _BudgetedStorage(self._inner.get_storage(model), self)

    @asynccontextmanager
    async def migration_lock(self):
        await self.acquire()
        try:
            async with self._inner.migration_lock():
                yield
        finally:
            self.release()


class _ServerBoom:
    """A server-side crash, read as the 500 a production deployment answers.

    The test app has no catch-all handler: an unhandled route exception
    propagates through the ASGI transport, where FastAPI's
    ``ServerErrorMiddleware`` would answer a 500 problem."""

    status_code = 500

    def __init__(self, exc: BaseException) -> None:
        self.text = f"server-side crash: {type(exc).__name__}: {exc}"


@pytest.mark.asyncio
async def test_twenty_concurrent_first_registrations_exhaust_a_small_pool(
    client, app, fake_storage_provider,
):
    """The ruling's test: 20 concurrent first registrations against a
    provider whose pool budget is two connections, while an external
    holder (another replica's migration, or another process's
    registration) keeps the provider's migration lock for 0.5 s.

    The pre-fix code let every registration wait on the lock while
    holding a pool connection: two waiters pin the whole two-connection
    budget while they wait, and the winner's insert needs a THIRD
    connection, so the pool runs out (500s). Now at most ONE
    registration waits on the lock (the rest queue on a per-app gate
    without a connection), so the budget of two always suffices:
    exactly one 200, the rest 409, none 500, one admin row."""
    app.state.storage_provider = _BudgetedProvider(fake_storage_provider)

    async def hold_the_lock() -> None:
        async with fake_storage_provider.migration_lock():
            await asyncio.sleep(0.5)

    async def _register_or_boom(i: int):
        try:
            return await _register(client, f"user-{i}")
        except Exception as exc:  # noqa: BLE001 - a crash is a 500 in production
            return _ServerBoom(exc)

    holder = asyncio.create_task(hold_the_lock())
    results = await asyncio.gather(
        *(_register_or_boom(i) for i in range(20)),
    )
    await holder
    codes = sorted(r.status_code for r in results)
    assert codes == [200] + [409] * 19, (
        f"the pool budget broke the registration storm: {codes} "
        f"({[r.text[:120] for r in results if r.status_code != 409]!r})"
    )
    for loser in (r for r in results if r.status_code == 409):
        assert loser.json()["extensions"]["error"] == "user_already_exists", loser.text

    users = list(
        (
            await fake_storage_provider.get_storage(User).list(
                OffsetPage(offset=0, length=30),
            )
        ).items
    )
    assert len(users) == 1, (
        f"the storm created {len(users)} user rows, want exactly one: {users!r}"
    )
    assert users[0].role == "admin"


@pytest.mark.asyncio
async def test_a_registration_waiting_past_the_bound_answers_503(
    client, fake_storage_provider, monkeypatch,
):
    """A holder keeping the provider's migration lock past the wait bound
    makes the registration answer 503 promptly (the client retries)
    instead of pinning a pool connection until the DB timeout (fix round
    1). The gate is released with the refusal, so a registration after
    the holder lets go still wins."""
    import primer.api.routers.auth as auth_router

    monkeypatch.setattr(auth_router, "REGISTRATION_LOCK_WAIT_S", 0.05)

    async def hold_the_lock() -> None:
        async with fake_storage_provider.migration_lock():
            await asyncio.sleep(1.5)

    holder = asyncio.create_task(hold_the_lock())
    started = time.perf_counter()
    r = await _register(client, "busy")
    elapsed = time.perf_counter() - started
    await holder

    assert r.status_code == 503, r.text
    assert r.json()["extensions"]["error"] == "registration_busy", r.text
    # Promptly: far under the holder's 1.5 s hold (the pre-fix code waited
    # out the whole hold, then answered 200). The hash runs before the
    # bound starts, so the bound itself is measured from after it.
    assert elapsed < 1.0, f"the 503 took {elapsed:.2f} s; want it promptly"

    # The refused call inserted nothing.
    storage = fake_storage_provider.get_storage(User)
    assert (await storage.list(OffsetPage(offset=0, length=10))).items == []

    # The gate was released with the refusal: the retry wins.
    again = await _register(client, "busy")
    assert again.status_code == 200, again.text


async def _hold_gate_for(gate: asyncio.Lock, seconds: float) -> None:
    await asyncio.sleep(seconds)
    gate.release()


@pytest.mark.asyncio
async def test_a_committed_insert_never_answers_503(
    client, fake_storage_provider, monkeypatch
):
    """X3.7: the lock deadline covers the lock WAIT only. If it expires
    while the critical section runs -- AFTER the insert has committed,
    for instance -- the winner still gets its 200 + session cookie, not
    a 503 that would leave the committed admin row unreachable (the
    retry would hit 409)."""
    import primer.api.routers.auth as auth_router

    monkeypatch.setattr(auth_router, "REGISTRATION_LOCK_WAIT_S", 0.05)
    storage = fake_storage_provider.get_storage(User)
    real_create = storage.create

    async def _commit_then_stall(item):
        await real_create(item)  # committed
        await asyncio.sleep(0.06)  # past the bound, AFTER the commit

    monkeypatch.setattr(storage, "create", _commit_then_stall)

    r = await _register(client, "winner")

    assert r.status_code == 200, (
        f"a committed insert answered 503 instead of 200: {r.text}"
    )
    assert "primer_session" in r.cookies
    users = list(
        (
            await fake_storage_provider.get_storage(User).list(
                OffsetPage(offset=0, length=10),
            )
        ).items
    )
    assert len(users) == 1 and users[0].username == "winner", (
        f"the winner's row is missing or duplicated: {users!r}"
    )


@pytest.mark.asyncio
async def test_a_registration_gate_wait_past_the_bound_answers_503(
    client, app, fake_storage_provider, monkeypatch
):
    """X3.7: the per-app registration GATE wait is bounded too: an
    unbounded ``gate.acquire()`` would let a held gate (a registration
    storm, or a stalled holder) park anonymous registrations forever.
    Past the bound the call answers the same retryable 503 promptly, and
    releasing the gate lets the next registration win."""
    import primer.api.routers.auth as auth_router

    monkeypatch.setattr(auth_router, "REGISTRATION_LOCK_WAIT_S", 0.05)

    gate = asyncio.Lock()
    app.state.registration_gate = gate
    await gate.acquire()  # hold it: the gate's turn is stuck elsewhere

    holder = asyncio.create_task(_hold_gate_for(gate, 1.0))
    started = time.perf_counter()
    r = await _register(client, "gated")
    elapsed = time.perf_counter() - started
    await holder

    assert r.status_code == 503, r.text
    assert r.json()["extensions"]["error"] == "registration_busy", r.text
    assert elapsed < 1.0, f"the 503 took {elapsed:.2f} s; want it promptly"

    # The refused call inserted nothing, and the gate is usable again.
    storage = fake_storage_provider.get_storage(User)
    assert (await storage.list(OffsetPage(offset=0, length=10))).items == []
    again = await _register(client, "gated")
    assert again.status_code == 200, again.text
