"""W3 concurrency parity on real SQLite and CI-owned Postgres connections.

Admin role/disabled edits deliberately do NOT bump the authentication epoch.
The same interleavings must preserve role eligibility and other-role state.
All PostgreSQL parameters use the mandatory storage CI lane's shared gate.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
import pytest
import pytest_asyncio
from pydantic import create_model

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.auth.tokens import sign_session
from primer.auth.user_writes import write_user_fields
from primer.model.except_ import ServerError
from primer.model.common import Identifiable
from primer.model.provider import PoolConfig, PostgresConfig, SqliteConfig
from primer.model.user import User
from primer.storage.postgres import PostgresStorageProvider
from primer.storage.sqlite import SqliteStorageProvider
from tests.pg_gate import explicit_port, postgres_marks, require_postgres_url

_WHAT = "the welcome atomic-write parity tests"


def build_app(provider):
    registry = ProviderRegistry(
        provider, llm_factory=lambda _: object(), embedder_factory=lambda _: object(),
        cross_encoder_factory=lambda _: object(), toolset_factory=lambda _: object(),
    )
    return create_test_app(storage_provider=provider, provider_registry=registry)


def signed_client(app, name):
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://welcome")
    client.cookies.set(app.state.config.auth.cookie_name, sign_session(
        user_id=name, username=name, secret=app.state.session_secret, epoch=0,
    ))
    return client


@pytest_asyncio.fixture(params=["sqlite", pytest.param("postgres", marks=postgres_marks(_WHAT))])
async def state(request, tmp_path):
    if request.param == "sqlite":
        config = SqliteConfig(path=tmp_path / "welcome.sqlite")
        provider = SqliteStorageProvider(config)
    else:
        url = urlparse(require_postgres_url(_WHAT))
        config = PostgresConfig(
            hostname=url.hostname or "localhost", port=explicit_port(url),
            username=url.username or "primer", password=url.password or "",
            database=(url.path or "/").lstrip("/") or "postgres",
            db_schema=f"w3{uuid.uuid4().hex[:16]}", pool=PoolConfig(min_size=1, max_size=4),
        )
        provider = PostgresStorageProvider(config)
    await provider.initialize()
    app = build_app(provider)
    users = provider.get_storage(User)
    for name, role in (("operator", "admin"), ("victim", "user")):
        await users.create(User(id=name, username=name, role=role, created_at=datetime.now(UTC)))
    admin, victim = signed_client(app, "operator"), signed_client(app, "victim")
    try:
        yield SimpleNamespace(provider=provider, config=config, app=app, users=users, admin=admin, victim=victim)
    finally:
        await admin.aclose()
        await victim.aclose()
        if request.param == "postgres":
            async with provider.pool.acquire() as conn:
                await conn.execute(f'DROP SCHEMA IF EXISTS "{provider.schema}" CASCADE')
        await provider.aclose()


def interleave_after_initial_read(monkeypatch, state, edit):
    """Pause after the welcome's real, unprotected read, then commit the editor.

    Both the original helper and the fixed route read here. Middleware and
    unrelated admin reads are excluded; the edit is an actual HTTP operation,
    not a fabricated epoch bump or a fake patch_if refusal.
    """
    original = state.users.get
    injected = False

    async def read(user_id, *, conn=None):
        nonlocal injected
        caller = sys._getframe(1).f_code.co_name
        current = await original(user_id, conn=conn)
        if user_id == "victim" and not injected and conn is None and caller in ("_write_welcome", "write_user_fields"):
            injected = True
            await edit()
        return current

    monkeypatch.setattr(state.users, "get", read)
    return lambda: injected


@pytest.mark.parametrize("action", ["acknowledge", "reopen"])
@pytest.mark.parametrize("edit,status", [({"role": "admin"}, 409), ({"disabled": True}, 403), ({"role": "restricted"}, 403)])
async def test_role_or_disable_change_without_an_epoch_bump_refuses_stale_welcome(state, monkeypatch, action, edit, status):
    initial = frozenset({"user", "admin"}) if action == "reopen" else frozenset()
    await write_user_fields(state.users, "victim", {"welcome_acked_roles": initial}, bump_epoch=False)

    async def change():
        response = await state.admin.patch("/v1/admin/users/victim", json={**edit, "email": "concurrent@example.com"})
        assert response.status_code == 200, response.text
        assert (await state.users.get("victim")).session_epoch == 0

    injected = interleave_after_initial_read(monkeypatch, state, change)
    response = await state.victim.post("/v1/auth/welcome/" + action, json={"role": "user"})
    assert injected()
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    row = await state.users.get("victim")
    assert row.welcome_acked_roles == initial
    assert row.session_epoch == 0 and row.email == "concurrent@example.com"
    assert row.role == edit.get("role", "user") and row.disabled == edit.get("disabled", False)


@pytest.mark.parametrize("action", ["acknowledge", "reopen"])
async def test_role_aba_preserves_the_other_roles_concurrent_action_without_bumping_epoch(state, monkeypatch, action):
    initial = frozenset({"user", "admin"}) if action == "reopen" else frozenset()
    await write_user_fields(state.users, "victim", {"welcome_acked_roles": initial}, bump_epoch=False)

    async def other_role():
        for role in ("admin", "user"):
            edited = await state.admin.patch("/v1/admin/users/victim", json={"role": role})
            assert edited.status_code == 200, edited.text
            assert (await state.users.get("victim")).session_epoch == 0
            if role == "admin":
                changed = await state.victim.post("/v1/auth/welcome/" + action, json={"role": "admin"})
                assert changed.status_code == 200, changed.text
                assert (await state.users.get("victim")).welcome_acked_roles == ({"admin"} if action == "acknowledge" else {"user"})

    injected = interleave_after_initial_read(monkeypatch, state, other_role)
    response = await state.victim.post("/v1/auth/welcome/" + action, json={"role": "user"})
    assert injected() and response.status_code == 200, response.text
    row = await state.users.get("victim")
    assert row.welcome_acked_roles == ({"user", "admin"} if action == "acknowledge" else frozenset())
    assert row.role == "user" and not row.disabled and row.session_epoch == 0


@pytest.mark.parametrize("writer", ["acknowledge", "reopen", "role", "disabled"])
async def test_a_second_connection_waits_for_the_welcome_transaction_and_keeps_its_write(state, monkeypatch, writer):
    """Real independent database connections, not only an asyncio mutex.

    Postgres verifies the exact blocking PID. SQLite uses a second provider
    and connection, so only its database write lock can delay that writer.
    """
    await write_user_fields(state.users, "victim", {"welcome_acked_roles": frozenset({"admin"})}, bump_epoch=False)
    peer = type(state.provider)(state.config)
    await peer.initialize()
    other = peer.get_storage(User)
    assert await other.get("victim") is not None  # prepare DDL before acquiring either row lock
    peer_client = signed_client(build_app(peer), "victim")
    locked, release, writing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    first_connections, second_connections = [], []
    first_patch, second_patch = state.users.patch_if, other.patch_if

    async def pause_after_lock(*args, **kwargs):
        saved = await first_patch(*args, **kwargs)
        first_connections.append(kwargs.get("conn"))
        if len(first_connections) == 1:
            assert saved is not None
            locked.set()
            await release.wait()
        return saved

    async def record_second(*args, **kwargs):
        second_connections.append(kwargs.get("conn"))
        writing.set()
        return await second_patch(*args, **kwargs)

    monkeypatch.setattr(state.users, "patch_if", pause_after_lock)
    monkeypatch.setattr(other, "patch_if", record_second)

    async def second_writer():
        if writer in ("acknowledge", "reopen"):
            return await peer_client.post("/v1/auth/welcome/" + writer, json={"role": "user"})
        # The same field-scoped primitive used by ordinary non-bumping admin
        # edits. Their actual HTTP route is covered by the pre-lock tests.
        async with peer.transaction() as conn:
            return await other.patch_if(
                "victim", {writer: "admin" if writer == "role" else True},
                where={"session_epoch": [0]}, conn=conn,
            )

    first = asyncio.create_task(state.victim.post("/v1/auth/welcome/acknowledge", json={"role": "user"}))
    second = None
    try:
        async with asyncio.timeout(30):
            await locked.wait()
            second = asyncio.create_task(second_writer())
            await writing.wait()
            if isinstance(state.provider, PostgresStorageProvider):
                async with state.provider.pool.acquire() as probe:
                    while not await probe.fetchval(
                        "SELECT $1 = ANY(pg_blocking_pids($2))",
                        first_connections[0].get_server_pid(), second_connections[0].get_server_pid(),
                    ):
                        assert not second.done(), "the second writer did not wait on the welcome row lock"
                        await asyncio.sleep(0.01)
            else:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(second), timeout=0.05)
            release.set()
            response = await first
            assert response.status_code == 200, response.text
            result = await second
            if writer in ("acknowledge", "reopen"):
                assert result.status_code == 200, result.text
            else:
                assert result is not None
    finally:
        release.set()
        tasks = [first, *([second] if second is not None else [])]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await peer_client.aclose()
        await peer.aclose()

    assert len(first_connections) == 2 and first_connections[0] is first_connections[1]
    assert first_connections[0] is not None, "locking write and actual write must share the outer transaction"
    row = await state.users.get("victim")
    assert row.welcome_acked_roles == ({"admin"} if writer == "reopen" else {"admin", "user"})
    assert row.session_epoch == 0
    assert row.role == ("admin" if writer == "role" else "user")
    assert row.disabled == (writer == "disabled")


async def test_a_failure_after_the_welcome_write_rolls_back_the_whole_transaction_and_can_retry(state, monkeypatch):
    before = (await state.users.get("victim")).model_dump()
    original = state.users.patch_if

    async def fail_after_write(user_id, patch=None, **kwargs):
        result = await original(user_id, patch, **kwargs)
        if patch and "welcome_acked_roles" in patch:
            raise ServerError("injected failure after the actual welcome update")
        return result

    monkeypatch.setattr(state.users, "patch_if", fail_after_write)
    failed = await state.victim.post("/v1/auth/welcome/acknowledge", json={"role": "user"})
    assert failed.status_code == 502, failed.text
    assert failed.headers["content-type"].startswith("application/problem+json")
    assert (await state.users.get("victim")).model_dump() == before
    monkeypatch.setattr(state.users, "patch_if", original)
    retried = await state.victim.post("/v1/auth/welcome/acknowledge", json={"role": "user"})
    assert retried.status_code == 200, retried.text
    assert (await state.users.get("victim")).welcome_acked_roles == {"user"}


async def test_an_old_json_user_without_epoch_or_welcome_keys_still_acknowledges(state):
    # A model-local view writes precisely the old JSON shape, using the same
    # storage table name and public storage API on each backend.
    legacy_user = create_model("User", __base__=Identifiable,
        username=(str, ...), created_at=(datetime, ...), role=(str, "user"), disabled=(bool, False),
    )
    await state.users.delete("victim")
    await state.provider.get_storage(legacy_user).create(legacy_user(
        id="victim", username="victim", created_at=datetime.now(UTC),
    ))
    response = await state.victim.post("/v1/auth/welcome/acknowledge", json={"role": "user"})
    assert response.status_code == 200, response.text
    row = await state.users.get("victim")
    assert row.session_epoch == 0 and row.welcome_acked_roles == {"user"}


@pytest.mark.parametrize("bumps,status", [(1, 200), (8, 409)])
async def test_a_concurrent_epoch_bump_before_lock_is_preserved_and_contention_is_bounded(state, monkeypatch, bumps, status):
    original_get, original_patch = state.users.get, state.users.patch_if
    injected = 0

    async def before_lock(user_id, *, conn=None):
        nonlocal injected
        caller = sys._getframe(1).f_code.co_name
        current = await original_get(user_id, conn=conn)
        if user_id == "victim" and conn is None and caller == "_write_welcome" and injected < bumps:
            injected += 1
            changed = await original_patch(user_id, {"session_epoch": current.session_epoch + 1}, where={"session_epoch": [current.session_epoch]})
            assert changed is not None
        return current

    monkeypatch.setattr(state.users, "get", before_lock)
    response = await state.victim.post("/v1/auth/welcome/acknowledge", json={"role": "user"})
    assert response.status_code == status, response.text
    assert injected == bumps
    row = await state.users.get("victim")
    assert row.session_epoch == bumps
    assert row.welcome_acked_roles == ({"user"} if status == 200 else frozenset())
    if status == 409:
        assert response.headers["content-type"].startswith("application/problem+json")
