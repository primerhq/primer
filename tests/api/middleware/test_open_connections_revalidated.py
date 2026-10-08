"""A connection that is ALREADY open is cut off when its account stops being valid (security ticket 01a11b97).

``AuthMiddleware`` read the user once, when a request or WebSocket opened. Nothing looked again, so disabling a compromised account, bumping
its ``session_epoch`` (sign-out-everywhere), revoking its API token or demoting it left every connection that account already held fully
alive: the terminal WebSocket (a real shell), the tap SSE stream, an MCP streamable-HTTP GET stream. A disabled account kept its shell.

The middleware now runs a bounded watcher beside any request that outlives ``auth.revalidate_interval_s`` (default 5 s): it re-reads the
user (and the API token for a bearer connection) from storage, which works across API processes without a bus, and when the account is
disabled or gone, its ``session_epoch`` moved (cookie sessions), its role changed, or its API token was revoked or expired, it cancels the
downstream app and ends the connection: a WebSocket closes 4401 ``auth_revoked``; an HTTP stream is completed cleanly. A request that
finishes before the first interval never has a watcher (no extra read). A storage error is tolerated for a bounded number of checks, then
fails closed. ``revalidate_interval_s = 0`` turns the watcher off. The per-request path is unchanged.

Each test body is bounded: a stream that does not end fails in seconds, it does not hang the suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.responses import StreamingResponse
from starlette.testclient import TestClient as SyncTestClient
from starlette.websockets import WebSocketDisconnect

from primer.auth.api_tokens import extract_prefix, hash_token, mint_plaintext
from primer.model.api_token import ApiToken
from primer.model.storage import OffsetPage
from primer.model.user import User
from tests.api.test_terminal import _LINUX, _build_app, _login, _login_non_admin  # noqa: F401
from tests.api.test_workspace_tap_sse import (  # noqa: F401  (fixtures are used by name)
    _SSEConnection,
    _ensure_workspace,
    _seed_session,
    _wire_tap_router,
    app,
    fake_provider_registry,
    workspace_registry,
)

INTERVAL = 0.1
WITHIN = 5.0          # an open connection must be gone within this many seconds of the account changing


async def _probe_stream():
    """An endless stream of small chunks, like an SSE or MCP GET stream; ends only when the server cancels it."""
    while True:
        yield b"tick\n"
        await asyncio.sleep(0.02)


@pytest.fixture
def probed(app):  # noqa: F811
    """The tap test app with a probe stream route and a short revalidation interval."""
    app.state.config.auth.revalidate_interval_s = INTERVAL

    async def stream():
        return StreamingResponse(_probe_stream(), media_type="text/event-stream")

    async def one_shot():
        return {"ok": True}

    app.add_api_route("/v1/_probe/stream", stream, methods=["GET"])
    app.add_api_route("/v1/_probe/one_shot", one_shot, methods=["GET"])

    # A MOUNTED sub-app, the way the MCP server is mounted: the middleware is outermost for the whole app, so its streams are covered too.
    from starlette.applications import Starlette
    from starlette.routing import Route

    async def mounted_stream(request):
        return StreamingResponse(_probe_stream(), media_type="text/event-stream")

    app.mount("/v1/_probe_mounted", Starlette(routes=[Route("/stream", mounted_stream)]))
    return app


class _Http:
    """One HTTP request driven straight through the ASGI app, recording what the app sends and when it ends."""

    def __init__(self, app, path: str, headers: list[tuple[bytes, bytes]], method: str = "GET", wait_for_start: bool = True) -> None:
        self.app, self.path, self.headers, self.method = app, path, headers, method
        self.wait_for_start = wait_for_start        # False for a request whose response starts only when its work is done
        self.messages: list[dict] = []
        self.task: asyncio.Task | None = None
        self.disconnect = asyncio.Event()
        self.error: BaseException | None = None

    async def __aenter__(self) -> "_Http":
        raw, _, query = self.path.partition("?")
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1", "method": self.method,
            "scheme": "http", "path": raw, "raw_path": raw.encode(), "query_string": query.encode(),
            "headers": [(b"host", b"test"), *self.headers], "client": ("127.0.0.1", 1), "server": ("test", 80), "app": self.app,
        }

        delivered = {"body": False}

        async def receive():
            if not delivered["body"]:
                delivered["body"] = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await self.disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            self.messages.append(message)

        async def run():
            try:
                await self.app(scope, receive, send)
            except BaseException as exc:  # noqa: BLE001 - recorded, asserted on by the tests that care
                self.error = exc
                if isinstance(exc, asyncio.CancelledError):
                    raise

        self.task = asyncio.create_task(run())
        deadline = time.monotonic() + WITHIN
        while self.wait_for_start and not any(m["type"] == "http.response.start" for m in self.messages):
            if self.task.done() or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc) -> None:
        self.disconnect.set()
        if self.task is not None and not self.task.done():
            self.task.cancel()
        if self.task is not None:
            with contextlib.suppress(BaseException):
                await self.task

    @property
    def status(self) -> int | None:
        return next((m["status"] for m in self.messages if m["type"] == "http.response.start"), None)

    @property
    def completed(self) -> bool:
        return any(m["type"] == "http.response.body" and not m.get("more_body", False) for m in self.messages)

    @property
    def chunks(self) -> int:
        return sum(1 for m in self.messages if m["type"] == "http.response.body" and m.get("body"))

    async def ended_within(self, seconds: float = WITHIN) -> bool:
        assert self.task is not None
        done, _ = await asyncio.wait({self.task}, timeout=seconds)
        return bool(done)


async def _until(predicate, seconds: float = WITHIN) -> bool:
    """Wait for ``predicate()`` to become true (polling every 10 ms); False when it did not within ``seconds``. Tests wait on COUNTS of checks, not on guesses of elapsed time."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def _cookie_header(cookie: str) -> list[tuple[bytes, bytes]]:
    return [(b"cookie", f"primer_session={cookie}".encode())]


async def _cookies(app) -> tuple[str, str]:
    """Register the first (admin) user and a second plain user; return their session cookies."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as admin:
        await admin.post("/v1/auth/register", json={"username": "tapuser", "password": "tappassword"})
        r = await admin.post("/v1/auth/login", json={"username": "tapuser", "password": "tappassword"})
        assert r.status_code == 200, r.text
        made = await admin.post("/v1/admin/users", json={"username": "other", "password": "otherpassword1", "role": "user"})
        assert made.status_code == 201, made.text
        admin_cookie = admin.cookies.get("primer_session")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        r = await other.post("/v1/auth/login", json={"username": "other", "password": "otherpassword1"})
        assert r.status_code == 200, r.text
        return admin_cookie, other.cookies.get("primer_session")


async def _user(app, username: str) -> User:
    page = await app.state.storage_provider.get_storage(User).list(OffsetPage(offset=0, length=50))
    return next(u for u in page.items if u.username == username)


async def _change(app, username: str, **changes) -> None:
    storage = app.state.storage_provider.get_storage(User)
    user = await _user(app, username)
    await storage.update(user.model_copy(update=changes))


async def _bearer_for(app, username: str) -> tuple[str, ApiToken]:
    user = await _user(app, username)
    plaintext = mint_plaintext()
    token = ApiToken(
        id="at-open", user_id=user.id, name="open stream", token_hash=hash_token(plaintext), prefix=extract_prefix(plaintext),
        scopes=["mcp"], created_at=datetime.now(timezone.utc),
    )
    await app.state.storage_provider.get_storage(ApiToken).create(token)
    return plaintext, token


# --- the generic mechanism, on a probe stream -------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_stream_ends_when_its_user_is_disabled(probed) -> None:
    admin_cookie, other_cookie = await _cookies(probed)
    async with _Http(probed, "/v1/_probe/stream", _cookie_header(other_cookie)) as conn:
        assert conn.status == 200
        await asyncio.sleep(INTERVAL * 3)
        assert not conn.task.done(), "the stream is open before anything changes"
        await _change(probed, "other", disabled=True)
        assert await conn.ended_within(), "a disabled account kept its open stream"
        assert conn.completed, "the response is completed cleanly, not left half-written"
        assert conn.error is None


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_stream_of_a_mounted_sub_app_ends_when_its_user_is_disabled(probed) -> None:
    """The MCP server is a mounted ASGI app whose GET streams are long-lived; the middleware wraps the mount, so they are cut off too."""
    _, other_cookie = await _cookies(probed)
    async with _Http(probed, "/v1/_probe_mounted/stream", _cookie_header(other_cookie)) as conn:
        assert conn.status == 200
        await asyncio.sleep(INTERVAL * 3)
        assert not conn.task.done()
        await _change(probed, "other", disabled=True)
        assert await conn.ended_within(), "a mounted app's open stream outlived its disabled user"


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_stream_ends_when_the_session_epoch_moves(probed) -> None:
    """Sign-out-everywhere (#495) bumps ``session_epoch``; the next request already fails, and an open stream must too."""
    _, other_cookie = await _cookies(probed)
    async with _Http(probed, "/v1/_probe/stream", _cookie_header(other_cookie)) as conn:
        assert conn.status == 200
        user = await _user(probed, "other")
        await _change(probed, "other", session_epoch=user.session_epoch + 1)
        assert await conn.ended_within(), "a signed-out-everywhere session kept its open stream"
        assert conn.completed


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_stream_ends_when_its_user_is_deleted(probed) -> None:
    _, other_cookie = await _cookies(probed)
    async with _Http(probed, "/v1/_probe/stream", _cookie_header(other_cookie)) as conn:
        assert conn.status == 200
        user = await _user(probed, "other")
        await probed.state.storage_provider.get_storage(User).delete(user.id)
        assert await conn.ended_within()


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_stream_ends_when_its_users_role_changes(probed) -> None:
    """A demoted admin keeps the authority the connection was opened with unless it is reopened and re-gated."""
    admin_cookie, _ = await _cookies(probed)
    async with _Http(probed, "/v1/_probe/stream", _cookie_header(admin_cookie)) as conn:
        assert conn.status == 200
        await _change(probed, "tapuser", role="user")
        assert await conn.ended_within()


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_another_users_stream_is_not_touched(probed) -> None:
    admin_cookie, other_cookie = await _cookies(probed)
    async with _Http(probed, "/v1/_probe/stream", _cookie_header(admin_cookie)) as safe:
        async with _Http(probed, "/v1/_probe/stream", _cookie_header(other_cookie)) as doomed:
            await _change(probed, "other", disabled=True)
            assert await doomed.ended_within()
        await asyncio.sleep(INTERVAL * 5)
        assert not safe.task.done(), "disabling one account closed another account's stream"
        assert safe.chunks > 0


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_bearer_stream_ends_when_its_token_is_revoked(probed) -> None:
    await _cookies(probed)
    plaintext, token = await _bearer_for(probed, "other")
    async with _Http(probed, "/v1/_probe/stream", [(b"authorization", f"Bearer {plaintext}".encode())]) as conn:
        assert conn.status == 200
        await asyncio.sleep(INTERVAL * 3)
        assert not conn.task.done()
        await probed.state.storage_provider.get_storage(ApiToken).update(token.model_copy(update={"revoked_at": datetime.now(timezone.utc)}))
        assert await conn.ended_within(), "a revoked API token kept its open stream"


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_bearer_stream_ends_when_its_token_expires(probed) -> None:
    await _cookies(probed)
    plaintext, token = await _bearer_for(probed, "other")
    soon = datetime.now(timezone.utc) + timedelta(seconds=0.5)
    await probed.state.storage_provider.get_storage(ApiToken).update(token.model_copy(update={"expires_at": soon}))
    async with _Http(probed, "/v1/_probe/stream", [(b"authorization", f"Bearer {plaintext}".encode())]) as conn:
        assert conn.status == 200
        assert await conn.ended_within(), "an API token that expired kept its open stream"


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_bearer_stream_ends_when_its_owner_is_disabled(probed) -> None:
    await _cookies(probed)
    plaintext, _ = await _bearer_for(probed, "other")
    async with _Http(probed, "/v1/_probe/stream", [(b"authorization", f"Bearer {plaintext}".encode())]) as conn:
        assert conn.status == 200
        await _change(probed, "other", disabled=True)
        assert await conn.ended_within()


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_bearer_stream_is_not_cut_by_a_session_epoch_bump(probed) -> None:
    """Bearer auth never looked at ``session_epoch`` on a request; an open bearer stream does not start to (the per-request rules are unchanged)."""
    await _cookies(probed)
    plaintext, _ = await _bearer_for(probed, "other")
    async with _Http(probed, "/v1/_probe/stream", [(b"authorization", f"Bearer {plaintext}".encode())]) as conn:
        user = await _user(probed, "other")
        await _change(probed, "other", session_epoch=user.session_epoch + 1)
        await asyncio.sleep(INTERVAL * 6)
        assert not conn.task.done()


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_request_that_finishes_before_the_first_interval_costs_no_extra_read(probed) -> None:
    admin_cookie, _ = await _cookies(probed)
    probed.state.config.auth.revalidate_interval_s = 30.0
    storage = probed.state.storage_provider.get_storage(User)
    reads: list[str] = []
    real_get = storage.get

    async def counting_get(entity_id):
        reads.append(entity_id)
        return await real_get(entity_id)

    storage.get = counting_get
    try:
        async with _Http(probed, "/v1/_probe/one_shot", _cookie_header(admin_cookie)) as conn:
            assert await conn.ended_within(2.0)
            assert conn.status == 200 and conn.completed
    finally:
        storage.get = real_get
    assert len(reads) == 1, f"the per-request path reads the user once and a short request starts no watcher: {reads}"


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_an_interval_of_zero_turns_the_watcher_off(probed) -> None:
    _, other_cookie = await _cookies(probed)
    probed.state.config.auth.revalidate_interval_s = 0
    async with _Http(probed, "/v1/_probe/stream", _cookie_header(other_cookie)) as conn:
        await _change(probed, "other", disabled=True)
        await asyncio.sleep(0.6)
        assert not conn.task.done(), "with the watcher off an open stream is left alone (the old behaviour)"


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_storage_error_is_tolerated_a_few_times_then_fails_closed(probed) -> None:
    """Counted, not timed: how many checks failed decides what must still be open, not how long the test slept."""
    _, other_cookie = await _cookies(probed)
    storage = probed.state.storage_provider.get_storage(User)
    real_get = storage.get
    seen = {"fail": False, "failed": 0, "good": 0}

    async def flaky_get(entity_id):
        if seen["fail"]:
            seen["failed"] += 1
            raise RuntimeError("storage unavailable")
        seen["good"] += 1
        return await real_get(entity_id)

    storage.get = flaky_get
    try:
        async with _Http(probed, "/v1/_probe/stream", _cookie_header(other_cookie)) as conn:
            seen["fail"] = True
            assert await _until(lambda: seen["failed"] >= 2), "the watcher never ran two checks"
            assert not conn.task.done(), "two failed checks are not enough to cut a connection off: one blip must not close an open shell"
            seen["fail"] = False
            good_before = seen["good"]
            assert await _until(lambda: seen["good"] >= good_before + 2), "the watcher stopped checking"
            assert not conn.task.done()
            seen["fail"], seen["failed"] = True, 0                  # a good check reset the count: it takes three NEW failures
            assert await conn.ended_within(), "storage that keeps failing is a connection nobody can vouch for: fail closed"
            assert seen["failed"] >= 3, f"it closed after {seen['failed']} failed checks"
    finally:
        storage.get = real_get


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_cancelling_the_request_cancels_the_app_beneath_the_watcher(probed) -> None:
    """The watcher runs the app in a task of its own; the server cancelling the request (a client that went away) must reach it."""
    _, other_cookie = await _cookies(probed)
    seen = {"cancelled": False}

    async def stream():
        async def gen():
            try:
                while True:
                    yield b"x"
                    await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                seen["cancelled"] = True
                raise
        return StreamingResponse(gen(), media_type="text/event-stream")

    probed.add_api_route("/v1/_probe/cancel_me", stream, methods=["GET"])
    conn = _Http(probed, "/v1/_probe/cancel_me", _cookie_header(other_cookie))
    await conn.__aenter__()
    await asyncio.sleep(INTERVAL * 3)            # past the first interval, so the watcher is running
    conn.task.cancel()
    with contextlib.suppress(BaseException):
        await conn.task
    await asyncio.sleep(0.2)
    assert seen["cancelled"], "the app beneath the watcher was left running"


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_an_app_that_raises_still_raises_through_the_watcher(probed) -> None:
    _, other_cookie = await _cookies(probed)

    async def boom():
        await asyncio.sleep(INTERVAL * 2)
        raise RuntimeError("the handler failed")

    probed.add_api_route("/v1/_probe/boom", boom, methods=["GET"])
    async with _Http(probed, "/v1/_probe/boom", _cookie_header(other_cookie)) as conn:
        await conn.ended_within()
        assert conn.status == 500, "an exception in a slow handler is still a 500, not swallowed by the watcher"


# --- follow-up: role direction, cookie lifetime, the cancel grace, writes in flight ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_stream_is_not_cut_when_its_user_is_promoted(probed) -> None:
    """A promotion adds authority; the connection was opened with less than the user now has, and nothing it holds is unsafe."""
    _, other_cookie = await _cookies(probed)
    async with _Http(probed, "/v1/_probe/stream", _cookie_header(other_cookie)) as conn:
        await _change(probed, "other", role="admin")
        await asyncio.sleep(INTERVAL * 8)
        assert not conn.task.done(), "a promotion disconnected the user"


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@pytest.mark.parametrize(("start", "end"), [("admin", "user"), ("admin", "restricted"), ("user", "restricted")])
async def test_a_stream_is_cut_on_every_kind_of_downgrade(probed, start: str, end: str) -> None:
    _, other_cookie = await _cookies(probed)
    await _change(probed, "other", role=start)                  # the role the connection is opened with
    async with _Http(probed, "/v1/_probe/stream", _cookie_header(other_cookie)) as conn:
        await _change(probed, "other", role=end)
        assert await conn.ended_within(), f"{start} -> {end} kept its open stream"


@pytest.mark.parametrize(("old", "new", "cut"), [
    ("admin", "user", True), ("admin", "restricted", True), ("user", "restricted", True),
    ("user", "admin", False), ("restricted", "user", False), ("restricted", "admin", False),
    ("user", "user", False),
    ("user", "superuser", True),          # a role the ranking does not know cannot be shown to be no weaker: fail closed
    ("superuser", "admin", True),         # nor can one it never knew
])
def test_which_role_changes_end_a_connection(old: str, new: str, cut: bool) -> None:
    from primer.api.middleware.revalidate import role_was_weakened

    assert role_was_weakened(old, new) is cut


def _eight_days_on(monkeypatch) -> None:
    """Move the revalidation clock past a 7-day cookie lifetime (the default ``session_ttl_days``)."""
    from primer.api.middleware import revalidate

    real = revalidate._now
    monkeypatch.setattr(revalidate, "_now", lambda: real() + timedelta(days=8))


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_cookie_stream_ends_when_the_cookies_own_lifetime_runs_out(probed, monkeypatch) -> None:
    """A cookie is valid for ``session_ttl_days`` from when it was issued, and a connection opened just before the end used to outlive it forever."""
    _, other_cookie = await _cookies(probed)
    async with _Http(probed, "/v1/_probe/stream", _cookie_header(other_cookie)) as conn:
        assert conn.status == 200
        await asyncio.sleep(INTERVAL * 3)
        assert not conn.task.done()
        _eight_days_on(monkeypatch)
        assert await conn.ended_within(), "a stream outlived the session cookie that opened it"
        assert conn.completed


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_bearer_stream_without_an_expiry_is_not_cut_by_the_cookie_lifetime(probed, monkeypatch) -> None:
    await _cookies(probed)
    plaintext, _ = await _bearer_for(probed, "other")
    async with _Http(probed, "/v1/_probe/stream", [(b"authorization", f"Bearer {plaintext}".encode())]) as conn:
        _eight_days_on(monkeypatch)
        await asyncio.sleep(INTERVAL * 8)
        assert not conn.task.done(), "an API token that never expires was treated like a cookie"


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_valid_cookie_of_a_gone_user_that_falls_through_to_a_bearer_does_not_lend_it_the_cookies_expiry(probed, monkeypatch) -> None:
    """The middleware tries the cookie first. A cookie that verifies but whose user row is gone authenticates nobody, so the request falls through to
    the bearer, and the cookie's lifetime (already read from its signature) must not follow it there: an API token has its own expiry."""
    admin_cookie, other_cookie = await _cookies(probed)
    plaintext, _ = await _bearer_for(probed, "tapuser")
    other = await _user(probed, "other")
    await probed.state.storage_provider.get_storage(User).delete(other.id)
    headers = [*_cookie_header(other_cookie), (b"authorization", f"Bearer {plaintext}".encode())]
    async with _Http(probed, "/v1/_probe/stream", headers) as conn:
        assert conn.status == 200, "the bearer authenticated the request"
        _eight_days_on(monkeypatch)
        await asyncio.sleep(INTERVAL * 8)
        assert not conn.task.done(), "the cookie's expiry was applied to a bearer connection"


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_an_app_that_outlives_the_cancel_grace_is_logged_at_debug(probed, monkeypatch, caplog) -> None:
    """The connection is closed over an app that has not finished unwinding; that is worth a debug line, not silence."""
    from primer.api.middleware import revalidate

    monkeypatch.setattr(revalidate, "_CANCEL_GRACE_S", 0.1)
    _, other_cookie = await _cookies(probed)
    finish, reached = asyncio.Event(), asyncio.Event()

    async def stubborn(scope, receive, send):                        # raw ASGI: one plain cancel, then a slow unwind
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"x", "more_body": True})
        try:
            reached.set()                                           # from here a cancel lands in the handler that swallows it
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            while not finish.is_set():                               # swallow every cancel (a task group re-delivers its own) until released
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    pass
            raise

    probed.mount("/v1/_probe_stubborn", stubborn)
    try:
        with caplog.at_level(logging.DEBUG, logger="primer.api.middleware.revalidate"):
            async with _Http(probed, "/v1/_probe_stubborn/s", _cookie_header(other_cookie)) as conn:
                assert conn.status == 200
                await asyncio.wait_for(reached.wait(), WITHIN)      # a cancel that lands BEFORE this unwinds at once and logs nothing (one flake seen under load)
                await _change(probed, "other", disabled=True)
                assert await conn.ended_within(), "the connection was held open by an app that would not unwind"
                lines = [r for r in caplog.records if "still running" in r.getMessage()]
                assert lines, "no line said the app outlived the grace"
                assert all(r.levelno == logging.DEBUG for r in lines)
    finally:
        finish.set()                                                # let the stubborn task end so nothing leaks past the test


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_write_in_flight_is_not_cancelled_by_a_storage_outage(probed) -> None:
    """Failing closed on a storage outage ends streams and sockets. A POST that is merely slow would be cancelled MID-WRITE, and storage being
    down is exactly when it fails by itself, so it is left alone."""
    _, other_cookie = await _cookies(probed)
    entered, finished = asyncio.Event(), asyncio.Event()

    async def slow_write():
        entered.set()
        await asyncio.sleep(INTERVAL * 12)
        finished.set()
        return {"ok": True}

    probed.add_api_route("/v1/_probe/slow_write", slow_write, methods=["POST"])
    storage = probed.state.storage_provider.get_storage(User)
    real_get = storage.get
    seen = {"failed": 0}

    async def failing_get(entity_id):
        seen["failed"] += 1
        raise RuntimeError("storage unavailable")

    async with _Http(probed, "/v1/_probe/slow_write", _cookie_header(other_cookie), method="POST", wait_for_start=False) as conn:
        await asyncio.wait_for(entered.wait(), WITHIN)             # the request is past the middleware's own read of the user
        storage.get = failing_get
        try:
            assert await _until(lambda: seen["failed"] >= 5, seconds=INTERVAL * 11), "the watcher stopped checking before five failed checks"
            assert await conn.ended_within(), "the write never ended"
        finally:
            storage.get = real_get
        assert finished.is_set(), "a write in flight was cancelled by a storage outage"
        assert conn.status == 200


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_write_in_flight_IS_cancelled_when_its_account_is_disabled(probed) -> None:
    """A real revocation is not a storage guess: it ends whatever the account was doing, writes included (documented)."""
    _, other_cookie = await _cookies(probed)

    entered = asyncio.Event()

    async def long_write():
        entered.set()
        await asyncio.sleep(30)
        return {"ok": True}

    probed.add_api_route("/v1/_probe/long_write", long_write, methods=["POST"])
    async with _Http(probed, "/v1/_probe/long_write", _cookie_header(other_cookie), method="POST", wait_for_start=False) as conn:
        await asyncio.wait_for(entered.wait(), WITHIN)
        await _change(probed, "other", disabled=True)
        assert await conn.ended_within()


# --- the real terminal WebSocket --------------------------------------------------------------------------------------------------------


def _drain_until_closed(ws) -> int | None:
    """Read frames until the server closes the socket and return its close code; the PTY's own output is ignored.

    ``receive()`` hands back the raw ``websocket.close`` message (only ``receive_text``/``receive_bytes`` raise), so look at the type."""
    for _ in range(5000):
        try:
            message = ws.receive()
        except WebSocketDisconnect as exc:
            return exc.code
        if message.get("type") == "websocket.close":
            return message.get("code")
    return None


@pytest.mark.skipif(not _LINUX, reason="PTY requires a POSIX/Linux pseudo-terminal")
@pytest.mark.timeout(60)
def test_a_terminal_websocket_closes_4401_when_its_user_is_disabled(fake_storage_provider, fake_provider_registry_t, tmp_path) -> None:
    app_ = _build_app(fake_storage_provider, fake_provider_registry_t, root=str(tmp_path))
    app_.state.config.auth.revalidate_interval_s = INTERVAL
    with SyncTestClient(app_) as sclient:
        _login(sclient)                                        # the first registrant: an admin, allowed a shell
        with sclient.websocket_connect("/v1/workspaces/ws-1/terminal") as ws:
            ws.send_bytes(b"echo before\n")
            started = time.monotonic()
            sclient.portal.call(lambda: _change(app_, "testuser", disabled=True))
            closed = _drain_until_closed(ws)
        assert closed == 4401, f"the shell stayed open: {closed!r}"
        assert time.monotonic() - started < WITHIN


@pytest.mark.skipif(not _LINUX, reason="PTY requires a POSIX/Linux pseudo-terminal")
@pytest.mark.timeout(60)
def test_a_revoked_terminal_leaves_no_reader_or_writer_task_behind(fake_storage_provider, fake_provider_registry_t, tmp_path) -> None:
    """The handler is cancelled while it waits on its two loops; it must cancel them itself, not leave them to die when the socket does."""
    app_ = _build_app(fake_storage_provider, fake_provider_registry_t, root=str(tmp_path))
    app_.state.config.auth.revalidate_interval_s = INTERVAL

    def loops() -> list[str]:
        return sorted(t.get_coro().__qualname__ for t in asyncio.all_tasks() if t.get_coro().__qualname__ in ("_recv_loop", "_send_loop"))

    async def alive():
        return loops()

    with SyncTestClient(app_) as sclient:
        _login(sclient)
        with sclient.websocket_connect("/v1/workspaces/ws-1/terminal") as ws:
            ws.send_bytes(b"echo up\n")
            assert sclient.portal.call(alive) == ["_recv_loop", "_send_loop"], "the terminal's two loops are running"
            sclient.portal.call(lambda: _change(app_, "testuser", disabled=True))
            assert _drain_until_closed(ws) == 4401
            time.sleep(0.3)
            assert sclient.portal.call(alive) == [], "the reader and writer tasks outlived the connection that was cut off"


@pytest.mark.skipif(not _LINUX, reason="PTY requires a POSIX/Linux pseudo-terminal")
@pytest.mark.timeout(60)
def test_a_terminal_websocket_closes_4401_when_the_session_epoch_moves(fake_storage_provider, fake_provider_registry_t, tmp_path) -> None:
    app_ = _build_app(fake_storage_provider, fake_provider_registry_t, root=str(tmp_path))
    app_.state.config.auth.revalidate_interval_s = INTERVAL
    with SyncTestClient(app_) as sclient:
        _login(sclient)
        with sclient.websocket_connect("/v1/workspaces/ws-1/terminal") as ws:
            ws.send_bytes(b"echo before\n")

            async def bump():
                user = await _user(app_, "testuser")
                await _change(app_, "testuser", session_epoch=user.session_epoch + 1)

            sclient.portal.call(bump)
            closed = _drain_until_closed(ws)
        assert closed == 4401


@pytest.mark.skipif(not _LINUX, reason="PTY requires a POSIX/Linux pseudo-terminal")
@pytest.mark.timeout(60)
def test_a_terminal_websocket_closes_when_an_admin_is_demoted(fake_storage_provider, fake_provider_registry_t, tmp_path) -> None:
    app_ = _build_app(fake_storage_provider, fake_provider_registry_t, root=str(tmp_path))
    app_.state.config.auth.revalidate_interval_s = INTERVAL
    with SyncTestClient(app_) as sclient:
        _login(sclient)
        with sclient.websocket_connect("/v1/workspaces/ws-1/terminal") as ws:
            sclient.portal.call(lambda: _change(app_, "testuser", role="user"))
            closed = _drain_until_closed(ws)
        assert closed == 4401


@pytest.mark.skipif(not _LINUX, reason="PTY requires a POSIX/Linux pseudo-terminal")
@pytest.mark.timeout(60)
def test_a_terminal_websocket_stays_open_while_the_account_is_valid(fake_storage_provider, fake_provider_registry_t, tmp_path) -> None:
    app_ = _build_app(fake_storage_provider, fake_provider_registry_t, root=str(tmp_path))
    app_.state.config.auth.revalidate_interval_s = INTERVAL
    with SyncTestClient(app_) as sclient:
        _login(sclient)
        with sclient.websocket_connect("/v1/workspaces/ws-1/terminal") as ws:
            time.sleep(INTERVAL * 8)                            # several checks go by
            ws.send_bytes(b"echo still-here\n")
            collected = b""
            for _ in range(100):
                collected += ws.receive_bytes()
                if b"still-here" in collected:
                    break
            assert b"still-here" in collected


@pytest.fixture
def fake_provider_registry_t(fake_storage_provider):
    from primer.api.registries import ProviderRegistry

    return ProviderRegistry(
        fake_storage_provider,
        llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda p: object(),
    )


# --- the real tap SSE stream ------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_the_tap_stream_ends_when_its_user_is_disabled(probed, workspace_registry) -> None:  # noqa: F811
    wid, sid = "w-tap", "s-tap"
    await _ensure_workspace(probed, workspace_registry, wid)
    await _seed_session(probed, workspace_id=wid, session_id=sid, last_seq=0)
    admin_cookie, other_cookie = await _cookies(probed)
    bus, router = await _wire_tap_router(probed)
    try:
        async with _Http(probed, f"/v1/workspaces/{wid}/tap", _cookie_header(other_cookie)) as conn:
            assert conn.status == 200, conn.messages[:1]
            await asyncio.sleep(INTERVAL * 3)
            assert not conn.task.done()
            await _change(probed, "other", disabled=True)
            assert await conn.ended_within(), "a disabled account kept its open tap stream"
            assert conn.completed
    finally:
        await router.aclose()
        await bus.aclose()


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_the_tap_stream_ends_when_the_session_epoch_moves(probed, workspace_registry) -> None:  # noqa: F811
    wid, sid = "w-tap2", "s-tap2"
    await _ensure_workspace(probed, workspace_registry, wid)
    await _seed_session(probed, workspace_id=wid, session_id=sid, last_seq=0)
    _, other_cookie = await _cookies(probed)
    bus, router = await _wire_tap_router(probed)
    try:
        async with _Http(probed, f"/v1/workspaces/{wid}/tap", _cookie_header(other_cookie)) as conn:
            assert conn.status == 200
            user = await _user(probed, "other")
            await _change(probed, "other", session_epoch=user.session_epoch + 1)
            assert await conn.ended_within()
    finally:
        await router.aclose()
        await bus.aclose()


# --- auth disabled: no users to watch ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_with_auth_off_nothing_is_watched(probed) -> None:
    probed.state.config.auth.enabled = False
    async with _Http(probed, "/v1/_probe/stream", []) as conn:
        assert conn.status == 200
        await asyncio.sleep(INTERVAL * 6)
        assert not conn.task.done()

