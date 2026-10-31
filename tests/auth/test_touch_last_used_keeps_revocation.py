"""The background last-used touch writes ``last_used_at`` and nothing else, so it can never undo a revoke (core review finding 4).

It used to write the whole token row read at authentication; a revoke landing between that read and the touch was put back to "not revoked".
"""
from __future__ import annotations

from datetime import UTC, datetime

from primer.api.middleware.auth import AuthMiddleware
from primer.api.routers._api_tokens_shared import revoke_token
from primer.model.api_token import ApiToken
from primer.model.providers.storage import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


async def _provider(tmp_path):
    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "t.sqlite")))
    await sp.initialize()
    await sp.get_storage(ApiToken).create(ApiToken(
        id="tok-1", user_id="u1", name="ci", token_hash="a" * 64, prefix="abcdefgh", created_at=datetime.now(UTC),
    ))
    return sp


async def test_a_revoke_between_the_auth_read_and_the_touch_stays_revoked(tmp_path):
    sp = await _provider(tmp_path)
    store = sp.get_storage(ApiToken)
    mw = AuthMiddleware(app=None)
    read_at_auth = await mw._find_by_hash(sp, "a" * 64)
    assert await revoke_token(store, await store.get("tok-1"))
    await mw._touch_last_used(sp, read_at_auth)
    final = await store.get("tok-1")
    assert final.revoked_at is not None
    await sp.aclose()


async def test_an_ordinary_touch_records_last_used_and_changes_nothing_else(tmp_path):
    sp = await _provider(tmp_path)
    store = sp.get_storage(ApiToken)
    before = await store.get("tok-1")
    assert before.last_used_at is None
    await AuthMiddleware(app=None)._touch_last_used(sp, before)
    after = await store.get("tok-1")
    assert after.last_used_at is not None
    assert after.model_copy(update={"last_used_at": None}) == before
    await sp.aclose()


async def test_touching_a_deleted_token_does_not_recreate_it(tmp_path):
    sp = await _provider(tmp_path)
    store = sp.get_storage(ApiToken)
    row = await store.get("tok-1")
    await store.delete("tok-1")
    await AuthMiddleware(app=None)._touch_last_used(sp, row)
    assert await store.get("tok-1") is None
    await sp.aclose()


# -- the other whole-row writers of a token row: revoke and rename write their own field only -------------------------------------------------


async def test_a_revoke_from_a_stale_snapshot_keeps_a_rename_that_landed_since(tmp_path):
    sp = await _provider(tmp_path)
    store = sp.get_storage(ApiToken)
    stale = await store.get("tok-1")
    await store.patch_if("tok-1", {"name": "renamed"}, where={"user_id": ["u1"]})
    assert await revoke_token(store, stale) is True
    row = await store.get("tok-1")
    assert row.revoked_at is not None and row.name == "renamed"
    assert await revoke_token(store, await store.get("tok-1")) is False  # idempotent: already revoked
    await sp.aclose()


async def test_a_second_revoke_of_a_stale_snapshot_reports_no_transition(tmp_path):
    sp = await _provider(tmp_path)
    store = sp.get_storage(ApiToken)
    stale_a, stale_b = await store.get("tok-1"), await store.get("tok-1")
    assert await revoke_token(store, stale_a) is True
    first = (await store.get("tok-1")).revoked_at
    assert await revoke_token(store, stale_b) is False  # the guard: already revoked by someone else
    assert (await store.get("tok-1")).revoked_at == first
    await sp.aclose()


async def test_a_rename_never_undoes_a_revoke_that_landed_since(tmp_path):
    from primer.api.routers.api_tokens import rename_token_row

    sp = await _provider(tmp_path)
    store = sp.get_storage(ApiToken)
    stale = await store.get("tok-1")
    assert await revoke_token(store, await store.get("tok-1"))
    saved = await rename_token_row(store, stale, "renamed")
    row = await store.get("tok-1")
    assert row.revoked_at is not None and row.name == "renamed" and saved.name == "renamed"
    await sp.aclose()
