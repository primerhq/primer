"""The arithmetic and the bounds of ``primer.auth.throttle.LoginThrottle``, on a fake clock (architecture review A-07).

``tests/auth/test_login_throttle.py`` pins the behaviour through the login route with the real clock; this file pins what a real
clock cannot do cheaply: the doubling and its cap, the refusal that does not extend the wait, the reset, the idle forgetting, and the
memory bound.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from primer.auth.throttle import LoginThrottle

# Re-export so pytest can resolve the fixtures used by the route tests below.
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def _attempts(throttle: LoginThrottle, n: int, who=("alice", "10.0.0.1")) -> list[int]:
    return [throttle.reserve(*who) for _ in range(n)]


def test_the_first_five_attempts_are_free_and_the_sixth_must_wait(clock):
    throttle = LoginThrottle(clock=clock)

    assert _attempts(throttle, 5) == [0, 0, 0, 0, 0]
    assert throttle.reserve("alice", "10.0.0.1") == 2


def test_each_attempt_made_after_a_wait_doubles_the_next_wait_up_to_the_cap(clock):
    throttle = LoginThrottle(clock=clock)
    _attempts(throttle, 5)

    waits = []
    for _ in range(11):
        waits.append(throttle.reserve("alice", "10.0.0.1"))
        clock.now += waits[-1]
        assert throttle.reserve("alice", "10.0.0.1") == 0, "the attempt after the wait is let through"
    assert waits == [2, 4, 8, 16, 32, 64, 128, 256, 512, 900, 900]


def test_a_refused_attempt_is_not_counted_and_does_not_extend_the_wait(clock):
    throttle = LoginThrottle(clock=clock)
    _attempts(throttle, 5)

    during = []
    for step in (0.0, 0.25, 0.5, 0.5, 0.5):  # binary-exact steps: 0, 0.25, 0.75, 1.25, 1.75 seconds into a 2 second wait
        clock.now += step
        during.append(throttle.reserve("alice", "10.0.0.1"))
    clock.now += 0.25  # exactly the end of the wait

    assert during == [2, 2, 2, 1, 1], "whole seconds, rounded up, never below one"
    assert throttle.reserve("alice", "10.0.0.1") == 0
    assert throttle.reserve("alice", "10.0.0.1") == 4, "five refusals did not count as attempts: this is attempt six, not eleven"


def test_a_success_forgets_the_key_so_the_free_attempts_come_back(clock):
    throttle = LoginThrottle(clock=clock)
    _attempts(throttle, 5)
    assert throttle.reserve("alice", "10.0.0.1") > 0

    throttle.succeeded("alice", "10.0.0.1")

    assert _attempts(throttle, 5) == [0, 0, 0, 0, 0]
    assert throttle.reserve("alice", "10.0.0.1") == 2


def test_a_key_idle_for_an_hour_starts_again_from_zero(clock):
    throttle = LoginThrottle(clock=clock)
    _attempts(throttle, 5)
    clock.now += 2
    assert throttle.reserve("alice", "10.0.0.1") == 0  # attempt six, wait 4
    clock.now += 3600

    assert _attempts(throttle, 4) == [0, 0, 0, 0], "the six old attempts are forgotten, so these are one to four"
    assert throttle.reserve("alice", "10.0.0.1") == 0, "attempt five of the new run"
    assert throttle.reserve("alice", "10.0.0.1") == 2


def test_the_key_is_the_username_and_the_address(clock):
    throttle = LoginThrottle(clock=clock)
    _attempts(throttle, 5)

    assert throttle.reserve("alice", "10.0.0.1") > 0
    assert throttle.reserve("bob", "10.0.0.1") == 0
    assert throttle.reserve("alice", "10.0.0.2") == 0


def test_the_table_never_grows_past_max_entries_and_evicts_an_idle_key_before_a_waiting_one(clock):
    throttle = LoginThrottle(clock=clock, max_entries=3)
    _attempts(throttle, 5, ("locked", "x"))  # oldest key, and waiting
    throttle.reserve("idle-1", "x")
    throttle.reserve("idle-2", "x")

    throttle.reserve("newcomer", "x")

    assert len(throttle) == 3
    assert throttle.reserve("locked", "x") > 0, "the waiting key survived the flood"
    assert len(throttle) == 3


def test_when_every_tracked_key_is_waiting_the_oldest_is_evicted(clock):
    throttle = LoginThrottle(clock=clock, max_entries=2)
    _attempts(throttle, 5, ("a", "x"))
    _attempts(throttle, 5, ("b", "x"))

    throttle.reserve("c", "x")

    assert len(throttle) == 2
    assert throttle.reserve("a", "x") == 0, "a was the oldest and was evicted, so it counts from zero"


@pytest.mark.parametrize(
    "kwargs",
    [{"free_attempts": 0}, {"max_entries": 0}, {"base_delay": 0}, {"base_delay": 10, "max_delay": 5}, {"forget_after": 0}],
)
def test_nonsense_settings_are_refused(kwargs):
    with pytest.raises(ValueError):
        LoginThrottle(**kwargs)


# --- through the route, on the fake clock -----------------------------------------------------------------------------


async def _register(client) -> None:
    resp = await client.post("/v1/auth/register", json={"username": "alice", "password": "supersecret"})
    assert resp.status_code == 200, resp.text
    client.cookies.clear()


@pytest.mark.asyncio
async def test_once_the_backoff_has_elapsed_the_next_attempt_is_examined_again(client, app, clock):
    app.state.login_throttle = LoginThrottle(clock=clock)
    await _register(client)
    for _ in range(5):
        assert (await client.post("/v1/auth/login", json={"username": "alice", "password": "WRONG"})).status_code == 401
    during = await client.post("/v1/auth/login", json={"username": "alice", "password": "supersecret"})

    clock.now += int(during.headers["retry-after"])
    after = await client.post("/v1/auth/login", json={"username": "alice", "password": "supersecret"})

    assert during.status_code == 429 and after.status_code == 200, (during.text, after.text)


@pytest.mark.asyncio
async def test_a_header_cannot_buy_a_fresh_key(client):
    """The key is the peer address the server sees; X-Forwarded-For is written by the client and is never read."""
    await _register(client)

    statuses = []
    for n in range(7):
        resp = await client.post(
            "/v1/auth/login", json={"username": "alice", "password": "WRONG"}, headers={"X-Forwarded-For": f"198.51.100.{n}"},
        )
        statuses.append(resp.status_code)

    assert statuses == [401] * 5 + [429] * 2


@pytest.mark.asyncio
async def test_the_refusal_is_documented_in_the_openapi_schema(app):
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        schema = (await c.get("/openapi.json")).json()

    login = schema["paths"]["/v1/auth/login"]["post"]["responses"]
    assert "429" in login and "401" in login
