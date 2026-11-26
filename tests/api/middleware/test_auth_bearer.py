"""Bearer-fallback path in AuthMiddleware — Spec §5.

Reuses the existing ``client`` / ``raw_client`` / ``fake_storage_provider``
fixtures from ``tests/api/conftest.py``. ``client`` auto-registers a
user named ``testuser`` and yields an ``httpx.AsyncClient`` that
carries the session cookie. For the bearer-only path we strip cookies
from the client's jar so the cookie path can't satisfy auth ahead of
the bearer check.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

import pytest

from primer.auth.api_tokens import (
    extract_prefix,
    hash_token,
    mint_plaintext,
)
from primer.model.api_token import ApiToken
from primer.model.storage import OffsetPage
from primer.model.user import User


async def _seeded_user(fake_storage_provider) -> User:
    """Pull the auto-registered testuser row out of the fake storage."""
    storage = fake_storage_provider.get_storage(User)
    page = await storage.list(OffsetPage(offset=0, length=10))
    items = list(page.items)
    assert items, "client fixture should auto-register a testuser"
    return items[0]


def _drop_session_cookie(client) -> None:
    """Strip the session cookie from the client jar so the bearer
    fallback is the only viable auth path on subsequent requests."""
    client.cookies.clear()


@pytest.mark.asyncio
async def test_bearer_unknown_token_no_user(client):
    """Unknown bearer → unauthenticated → require_auth route returns 401."""
    _drop_session_cookie(client)
    resp = await client.get(
        "/v1/agents",
        headers={"Authorization": "Bearer primer_pat_nonexistent"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bearer_valid_token_authenticates(client, fake_storage_provider):
    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    api_token = ApiToken(
        id="at-1",
        user_id=user.id,
        name="test",
        token_hash=hash_token(plaintext),
        prefix=extract_prefix(plaintext),
        scopes=["mcp"],
        created_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(ApiToken).create(api_token)
    _drop_session_cookie(client)
    resp = await client.get(
        "/v1/agents",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_bearer_revoked_token_rejected(client, fake_storage_provider):
    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    api_token = ApiToken(
        id="at-2",
        user_id=user.id,
        name="test",
        token_hash=hash_token(plaintext),
        prefix=extract_prefix(plaintext),
        scopes=["mcp"],
        created_at=datetime.now(timezone.utc),
        revoked_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(ApiToken).create(api_token)
    _drop_session_cookie(client)
    resp = await client.get(
        "/v1/agents",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bearer_expired_token_rejected(client, fake_storage_provider):
    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    api_token = ApiToken(
        id="at-3",
        user_id=user.id,
        name="test",
        token_hash=hash_token(plaintext),
        prefix=extract_prefix(plaintext),
        scopes=["mcp"],
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    await fake_storage_provider.get_storage(ApiToken).create(api_token)
    _drop_session_cookie(client)
    resp = await client.get(
        "/v1/agents",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bearer_wrong_prefix_ignored(client):
    """Bearer that doesn't start with ``primer_pat_`` is ignored."""
    _drop_session_cookie(client)
    resp = await client.get(
        "/v1/agents",
        headers={"Authorization": "Bearer some-random-string"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_cookie_path_unchanged(client):
    """Cookie auth must still work after the bearer fallback was added."""
    # The ``client`` fixture already set the session cookie via /register.
    resp = await client.get("/v1/agents")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_find_by_hash_uses_cursor_page_no_count():
    """The bearer lookup must page via CursorPage, not OffsetPage.

    The offset path issues an extra COUNT(*) on every bearer-authenticated
    request (E-I2); the keyset cursor path issues none. Assert the page
    type the storage receives so the COUNT can't silently regress.
    """
    from primer.api.middleware.auth import AuthMiddleware
    from primer.model.storage import CursorPage, CursorPageResponse

    seen_pages: list = []

    class _SpyTokenStorage:
        async def find(self, predicate, page, *, order_by=None):
            seen_pages.append(page)
            return CursorPageResponse(next_cursor=None, items=[])

    class _SpyProvider:
        def get_storage(self, model_class):
            return _SpyTokenStorage()

    mw = AuthMiddleware(app=None)
    result = await mw._find_by_hash(_SpyProvider(), "a" * 64)

    assert result is None
    assert len(seen_pages) == 1
    assert isinstance(seen_pages[0], CursorPage)
    assert seen_pages[0].length == 1


@pytest.mark.asyncio
async def test_bearer_updates_last_used_at(client, fake_storage_provider):
    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    api_token = ApiToken(
        id="at-touch",
        user_id=user.id,
        name="touch",
        token_hash=hash_token(plaintext),
        prefix=extract_prefix(plaintext),
        scopes=["mcp"],
        created_at=datetime.now(timezone.utc),
        last_used_at=None,
    )
    storage = fake_storage_provider.get_storage(ApiToken)
    await storage.create(api_token)
    _drop_session_cookie(client)
    resp = await client.get(
        "/v1/agents",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200
    # Give the fire-and-forget task a chance to run.
    for _ in range(20):
        refreshed = await storage.get(api_token.id)
        if refreshed is not None and refreshed.last_used_at is not None:
            break
        await asyncio.sleep(0.05)
    refreshed = await storage.get(api_token.id)
    assert refreshed is not None
    assert refreshed.last_used_at is not None


@pytest.mark.asyncio
async def test_bearer_last_used_at_write_is_throttled(client, fake_storage_provider, monkeypatch):
    """Review 4.6: a fresh last_used_at stamp (written within the interval)
    is NOT rewritten by the next bearer request; a stale one (older than
    the interval) is. N requests a second must not cause N UPDATEs a second
    on the same row."""
    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    storage = fake_storage_provider.get_storage(ApiToken)
    fresh = datetime.now(timezone.utc)
    await storage.create(
        ApiToken(
            id="at-throttled",
            user_id=user.id,
            name="throttled",
            token_hash=hash_token(plaintext),
            prefix=extract_prefix(plaintext),
            scopes=["mcp"],
            created_at=fresh,
            last_used_at=fresh.isoformat(),
        )
    )
    _drop_session_cookie(client)

    # Spy the stamp writes of this token.
    writes = []
    real_patch = storage.patch_if

    async def _spy(token_id, fields, where=None):
        if token_id == "at-throttled":
            writes.append(fields)
        return await real_patch(token_id, fields, where=where)

    monkeypatch.setattr(storage, "patch_if", _spy)

    # Fresh stamp: the request must not rewrite it.
    resp = await client.get(
        "/v1/agents", headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200
    for _ in range(20):
        await asyncio.sleep(0.05)
    assert writes == [], (
        f"a fresh last_used_at stamp was rewritten by the next request: {writes!r}"
    )

    # Stale stamp (older than the interval): the request rewrites it once.
    stale = (fresh - timedelta(minutes=2)).isoformat()
    written = await real_patch(
        "at-throttled", {"last_used_at": stale}, where={"revoked_at": [None]},
    )
    assert written is not None
    resp = await client.get(
        "/v1/agents", headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200
    for _ in range(20):
        if writes:
            break
        await asyncio.sleep(0.05)
    assert len(writes) == 1 and writes[0].get("last_used_at") != stale, (
        "a stale last_used_at stamp was not rewritten by the request "
        f"(writes: {writes!r})"
    )


@pytest.mark.asyncio
async def test_touch_last_used_error_is_logged(client, fake_storage_provider, monkeypatch, caplog):
    """Review 4.6: a failed last_used_at write is logged at warning (with
    the traceback), not swallowed at debug."""
    import logging

    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    storage = fake_storage_provider.get_storage(ApiToken)
    await storage.create(
        ApiToken(
            id="at-errlog",
            user_id=user.id,
            name="errlog",
            token_hash=hash_token(plaintext),
            prefix=extract_prefix(plaintext),
            scopes=["mcp"],
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
        )
    )

    async def _boom(*args, **kwargs):
        raise RuntimeError("storage down")

    monkeypatch.setattr(storage, "patch_if", _boom)
    _drop_session_cookie(client)
    with caplog.at_level(logging.WARNING, logger="primer.api.middleware.auth"):
        resp = await client.get(
            "/v1/agents", headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert resp.status_code == 200
    # Give the fire-and-forget task a chance to fail and be logged.
    for _ in range(20):
        if any(
            "touch_last_used" in record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ):
            break
        await asyncio.sleep(0.05)

    # The request itself is unaffected, and the failure must be visible:
    # a warning naming the touch (not swallowed at debug).
    assert resp.status_code == 200
    assert any(
        "touch_last_used" in record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ), (
        f"a failed last_used_at write left no warning record "
        f"(records: {[r.getMessage() for r in caplog.records]!r})"
    )


# --- Review round 1 part 2 (4.6: the throttle must not stack writes) -------
#
# The row stamp only moved when a write completed, so a slow or failing
# store started a fresh write task on EVERY bearer request (the task set
# grew without bound on a slow DB) and a failing store logged a WARNING +
# traceback per request. The middleware now keeps in-process in-flight and
# last-attempt records per token id: at most one write per token in flight
# per window, and a failing write logs at most one WARNING per window.


async def _bearer_agent_request(client, plaintext: str):
    return await client.get(
        "/v1/agents", headers={"Authorization": f"Bearer {plaintext}"},
    )


@pytest.mark.asyncio
async def test_bearer_slow_store_starts_one_write(
    client, fake_storage_provider, monkeypatch
):
    """Review 4.6 round 2: while one last_used_at write is in flight on a
    slow store, concurrent bearer requests start NO further write (the
    in-flight record holds the window)."""
    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    storage = fake_storage_provider.get_storage(ApiToken)
    await storage.create(
        ApiToken(
            id="at-slow",
            user_id=user.id,
            name="slow",
            token_hash=hash_token(plaintext),
            prefix=extract_prefix(plaintext),
            scopes=["mcp"],
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
        )
    )

    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def _slow_patch_if(token_id, values, where=None):
        calls.append(token_id)
        started.set()
        await release.wait()
        return values

    monkeypatch.setattr(storage, "patch_if", _slow_patch_if)
    _drop_session_cookie(client)

    results = await asyncio.gather(
        *[_bearer_agent_request(client, plaintext) for _ in range(5)]
    )
    assert all(r.status_code == 200 for r in results), (
        f"a slow touch write must not affect the request itself: {results!r}"
    )
    for _ in range(40):
        if started.is_set():
            break
        await asyncio.sleep(0.05)
    assert started.is_set(), "the touch write task never started"
    # The first write is still in flight when the other four requests ran:
    # none of them may have started a second write.
    assert len(calls) == 1, (
        f"concurrent bearer requests started {len(calls)} writes while one "
        f"was in flight (calls: {calls!r})"
    )
    release.set()
    # Let the in-flight write task complete so nothing is left pending on
    # the loop (the stub does not persist, so the row stamp stays as seeded;
    # the persistence semantics are pinned by the plain touch test above).
    for _ in range(20):
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_bearer_failing_store_logs_one_warning_per_window(
    client, fake_storage_provider, monkeypatch, caplog
):
    """Review 4.6 round 2: a failing store answers EVERY request, so the
    touch failure must log at most one WARNING + traceback per token per
    window (the in-window attempts log at debug)."""
    import logging

    user = await _seeded_user(fake_storage_provider)
    plaintext = mint_plaintext()
    storage = fake_storage_provider.get_storage(ApiToken)
    await storage.create(
        ApiToken(
            id="at-fail",
            user_id=user.id,
            name="fail",
            token_hash=hash_token(plaintext),
            prefix=extract_prefix(plaintext),
            scopes=["mcp"],
            created_at=datetime.now(timezone.utc),
            last_used_at=None,
        )
    )

    attempts: list[str] = []

    async def _boom(token_id, values, where=None):
        attempts.append(token_id)
        raise RuntimeError("storage down")

    monkeypatch.setattr(storage, "patch_if", _boom)
    _drop_session_cookie(client)

    with caplog.at_level(logging.WARNING, logger="primer.api.middleware.auth"):
        results = await asyncio.gather(
            *[_bearer_agent_request(client, plaintext) for _ in range(5)]
        )
    assert all(r.status_code == 200 for r in results), (
        f"a failing touch write must not affect the request itself: {results!r}"
    )
    # Let every attempt run and be logged (or suppressed).
    for _ in range(40):
        if len(attempts) >= 5:
            break
        await asyncio.sleep(0.05)
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and "touch_last_used" in record.getMessage()
    ]
    # A racing done-callback can start a second attempt, but the SECOND
    # warning in the window must be suppressed: exactly one WARNING.
    assert len(warnings) == 1, (
        f"a failing store logged {len(warnings)} warnings in one window, "
        f"want one (messages: {warnings!r})"
    )


async def _touch_middleware_setup(
    fake_storage_provider, monkeypatch, cap: int, *, failing_store: bool = False,
    hanging_store: bool = False,
):
    """Build the middleware around the REAL bearer path (real storage
    lookups, real touch tasks) and mint ``cap + 4`` valid tokens, each
    with a never-touched last_used_at so every request starts a touch.

    ``hanging_store=True`` makes the touch write never return (its
    in-flight record never evicts). Returns (middleware, drive,
    plaintexts, storage): ``drive(plaintext, wait=...)`` sends one bearer
    request through ``AuthMiddleware.__call__`` and, unless ``wait=False``,
    awaits that request's touch task to completion.
    """
    from types import SimpleNamespace

    import primer.api.middleware.auth as auth_mod

    assert auth_mod._TOUCH_RECORD_MAX >= 512, (
        "the default cap is sized for real deployments, not a test toy"
    )
    monkeypatch.setattr(auth_mod, "_TOUCH_RECORD_MAX", cap)

    # The direct drive does not go through the app, so the user row is
    # created here (the ``client`` fixture would otherwise auto-register
    # it).
    user = User(
        id="at-bounded-owner",
        username="bounded-owner",
        role="user",
        created_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(User).create(user)
    storage = fake_storage_provider.get_storage(ApiToken)
    now = datetime.now(timezone.utc)
    plaintexts = []
    for i in range(cap + 4):
        plaintext = mint_plaintext()
        plaintexts.append(plaintext)
        await storage.create(
            ApiToken(
                id=f"at-bounded-{i}",
                user_id=user.id,
                name=f"bounded-{i}",
                token_hash=hash_token(plaintext),
                prefix=extract_prefix(plaintext),
                scopes=["mcp"],
                created_at=now,
                last_used_at=None,
            )
        )

    if failing_store:
        async def _boom(token_id, values, where=None):
            raise RuntimeError("storage down")

        monkeypatch.setattr(storage, "patch_if", _boom)

    if hanging_store:
        async def _hang(token_id, values, where=None):
            await asyncio.Event().wait()

        monkeypatch.setattr(storage, "patch_if", _hang)

    async def _inner_app(scope, receive, send):
        return None

    config = SimpleNamespace(
        auth=SimpleNamespace(
            enabled=True,
            cookie_name="primer_session",
            session_ttl_days=7,
            revalidate_interval_s=0,
        )
    )
    app_ns = SimpleNamespace(
        state=SimpleNamespace(
            config=config,
            storage_provider=fake_storage_provider,
            session_secret=None,  # the cookie path returns before touching storage
        )
    )
    mw = auth_mod.AuthMiddleware(_inner_app)

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        return None

    async def drive(plaintext, *, wait: bool = True):
        scope = {
            "type": "http",
            "app": app_ns,
            "headers": [
                (b"authorization", f"Bearer {plaintext}".encode("latin-1")),
            ],
        }
        await mw(scope, _receive, _send)
        assert scope["state"].api_token is not None, (
            f"the token did not authenticate: {scope['state'].api_token!r}"
        )
        if wait:
            # Let this request's fire-and-forget touch run to completion (the
            # done callback is where the stamp/failure records move).
            await asyncio.gather(*list(mw._touch_tasks))

    return mw, drive, plaintexts, storage


@pytest.mark.asyncio
async def test_touch_stamp_records_are_bounded_across_distinct_tokens(
    fake_storage_provider, monkeypatch
):
    """X3.6: the per-token touch records are bounded, not one row per
    token EVER used. More distinct bearer tokens than the cap pass
    through the real bearer path; the stamp records stay at the cap, and
    the most recently used token keeps its record (oldest go first)."""
    cap = 8
    mw, drive, plaintexts, _storage = await _touch_middleware_setup(
        fake_storage_provider, monkeypatch, cap
    )

    for plaintext in plaintexts:
        await drive(plaintext)

    assert len(mw._touch_last_attempt) <= cap, (
        f"the stamp records kept one row per token ever used: "
        f"{len(mw._touch_last_attempt)} > {cap}"
    )
    assert f"at-bounded-{cap + 3}" in mw._touch_last_attempt, (
        "the most recently used token keeps its stamp record "
        f"(records: {sorted(mw._touch_last_attempt)!r})"
    )


@pytest.mark.asyncio
async def test_touch_failure_records_are_bounded_across_distinct_tokens(
    fake_storage_provider, monkeypatch
):
    """X3.6: the failure records are pruned with the stamp records -- a
    store that fails every touch write keeps at most the cap of per-token
    failure rows, not one per token ever used."""
    cap = 8
    mw, drive, plaintexts, _storage = await _touch_middleware_setup(
        fake_storage_provider, monkeypatch, cap, failing_store=True
    )

    for plaintext in plaintexts:
        await drive(plaintext)

    assert len(mw._touch_last_failure) <= cap, (
        f"the failure records kept one row per token ever used: "
        f"{len(mw._touch_last_failure)} > {cap}"
    )


@pytest.mark.asyncio
async def test_inflight_touch_records_are_bounded_when_the_store_never_returns(
    fake_storage_provider, monkeypatch
):
    """X3 round 1, B-M2: in-flight touch records evict only when their
    task completes, so a store whose writes never return would otherwise keep
    one in-flight entry per distinct token. At the cap, a token with no
    in-flight write of its own starts none (the stamp is best-effort and the
    next request retries), and the requests still authenticate."""
    cap = 4
    mw, drive, plaintexts, _storage = await _touch_middleware_setup(
        fake_storage_provider, monkeypatch, cap, hanging_store=True
    )

    for plaintext in plaintexts:
        await drive(plaintext, wait=False)

    assert len(mw._touch_inflight) <= cap, (
        f"the in-flight records kept one row per distinct token even though "
        f"no write ever completed: {len(mw._touch_inflight)} > {cap}"
    )
    for task in list(mw._touch_tasks):  # the hung writes end with the test
        task.cancel()
    await asyncio.gather(*list(mw._touch_tasks), return_exceptions=True)
