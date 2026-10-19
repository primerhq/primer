"""The OAuth store: ciphertext only at rest, a compare-and-set token row, single-use pending sign-ins (plan 07, Task 5; design 12.5 and 12.6).

Every test runs twice: over the in-memory fake the suite uses everywhere, and over a real SQLite database, whose ``patch_if`` is the production SQL, so the
canonical forms a guard compares (a ``version``, a lease timestamp, the ``consumed`` flag) are checked against a real backend too. The Postgres race twin
is ``tests/storage/test_mcp_oauth_store_live.py``.

The properties that matter, in the order the review reads them:

* what reaches the database is ciphertext: no token, client secret or PKCE verifier appears in a stored document;
* a pending sign-in is single use: of any number of concurrent ``take_pending`` calls, exactly one gets the row, and an expired row is never given out;
* the token row moves only by compare-and-set: a stale guard changes nothing, a connect never loses to or overwrites a version someone else bumped, and a
  patch that writes a timestamp leaves it in the form a later guard compares.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from pydantic import SecretStr

from primer.common.secret_box import SecretBox, SecretBoxError
from primer.model.common import dump_for_storage
from primer.model.mcp_oauth import McpOAuthClient, McpOAuthPendingSignIn, McpOAuthTokenSet
from primer.model.provider import SqliteConfig
from primer.storage import raw_generation
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.oauth.store import McpOAuthStore, pending_key

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
KEY = "store-test-key-0123456789"


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def provider(request, fake_storage_provider, tmp_path):
    if request.param == "memory":
        yield fake_storage_provider
        return
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "oauth.sqlite"))
    await sp.initialize()
    try:
        yield sp
    finally:
        await sp.aclose()


@pytest.fixture
def store(provider) -> McpOAuthStore:
    return McpOAuthStore(provider, SecretBox.from_key_material(KEY))


def token_row(store: McpOAuthStore, toolset_id: str = "kb", **over) -> McpOAuthTokenSet:
    base = {
        "id": toolset_id,
        "grant": "authorization_code",
        "access_token_enc": store.seal("tok-plain"),
        "refresh_token_enc": store.seal("ref-plain"),
        "expires_at": NOW + timedelta(hours=1),
        "scope": "mcp",
        "issuer": "https://as.example",
        "token_endpoint": "https://as.example/token",
        "revocation_endpoint": "https://as.example/revoke",
        "resource": "https://m.example/mcp",
        "client_id": "client-1",
        "client_source": "registered",
        "config_fingerprint": "f" * 64,
        "connected_at": NOW,
        "connected_by": "admin",
    }
    base.update(over)
    return McpOAuthTokenSet(**base)


def pending_row(store: McpOAuthStore, state: str = "state-1", toolset_id: str = "kb", **over) -> McpOAuthPendingSignIn:
    base = {
        "id": state,
        "toolset_id": toolset_id,
        "code_verifier_enc": store.seal("verifier-plain"),
        "redirect_uri": "https://p.example/v1/toolsets/kb/oauth/callback",
        "issuer": "https://as.example",
        "token_endpoint": "https://as.example/token",
        "client_id": "client-1",
        "client_source": "registered",
        "config_fingerprint": "f" * 64,
        "started_by": "admin",
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
    }
    base.update(over)
    return McpOAuthPendingSignIn(**base)


# ---- sealing ----------------------------------------------------------------------------------------------------------------------------------------------------------------


async def test_seal_and_unseal_round_trip_and_hide_the_ciphertext(store):
    sealed = store.seal("tok-plain")
    assert isinstance(sealed, SecretStr)
    assert "tok-plain" not in repr(sealed) and sealed.get_secret_value().startswith("v1.")
    assert store.unseal(sealed) == "tok-plain"
    assert store.unseal(None) is None


async def test_unseal_with_another_key_raises(store, provider):
    row = await store.replace_tokens(token_row(store))
    other = McpOAuthStore(provider, SecretBox.from_key_material("another-key-0123456789"))
    with pytest.raises(SecretBoxError):
        other.unseal(row.access_token_enc)


async def test_the_stored_document_holds_ciphertext_only(store):
    await store.replace_tokens(token_row(store))
    stored = await store.get_tokens("kb")
    document = json.dumps(dump_for_storage(stored))
    assert "tok-plain" not in document and "ref-plain" not in document
    assert store.unseal(stored.access_token_enc) == "tok-plain" and store.unseal(stored.refresh_token_enc) == "ref-plain"


async def test_the_database_holds_no_plaintext_in_any_of_the_three_tables(store, provider, tmp_path):
    """What is at rest, read back raw: the token, the client secret and the PKCE verifier appear nowhere in the stored documents."""
    await store.replace_tokens(token_row(store))
    await store.put_client(McpOAuthClient(id="kb", issuer="https://as.example", redirect_uri="https://p.example/cb", client_id="c1",
                                          client_secret_enc=store.seal("client-secret-plain"), registered_at=NOW))
    await store.put_pending(pending_row(store))          # stays: taking it would delete the row this test reads back
    stored = {
        "tokens": json.dumps(dump_for_storage(await store.get_tokens("kb"))),
        "client": json.dumps(dump_for_storage(await store.get_client("kb"))),
        "pending": json.dumps(dump_for_storage(await provider.get_storage(McpOAuthPendingSignIn).get(pending_key("state-1")))),
    }
    for name, document in stored.items():
        for plain in ("tok-plain", "ref-plain", "client-secret-plain", "verifier-plain", "state-1"):
            assert plain not in document, f"{plain} is in the stored {name} document"
    if not isinstance(provider, SqliteStorageProvider):
        return
    connection = sqlite3.connect(tmp_path / "oauth.sqlite")
    try:
        tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'mcpoauth%'")]
        assert sorted(tables) == ["mcpoauthclient", "mcpoauthpendingsignin", "mcpoauthtokenset"]
        rows = {table: [str(row) for row in connection.execute(f'SELECT * FROM "{table}"')] for table in tables}
    finally:
        connection.close()
    assert all(rows.values()), f"a table was empty, so its check proved nothing: {rows.keys()}"
    raw = " ".join(row for table_rows in rows.values() for row in table_rows)
    for plain in ("tok-plain", "ref-plain", "client-secret-plain", "verifier-plain", "state-1"):
        assert plain not in raw


# ---- plaintext never reaches storage ----------------------------------------------------------------------------------------------------------------------------------------


async def test_a_token_row_with_plaintext_is_refused_before_it_is_written(store, provider):
    with pytest.raises(ValueError, match="access_token_enc must hold a sealed value"):
        await store.replace_tokens(token_row(store, access_token_enc=SecretStr("tok-plain-by-mistake")))
    with pytest.raises(ValueError, match="refresh_token_enc"):
        await store.replace_tokens(token_row(store, refresh_token_enc=SecretStr("ref-plain-by-mistake")))
    assert await store.get_tokens("kb") is None


async def test_a_client_secret_or_verifier_in_plaintext_is_refused(store):
    with pytest.raises(ValueError, match="client_secret_enc"):
        await store.put_client(McpOAuthClient(id="kb", issuer="https://as.example", redirect_uri="https://p.example/cb", client_id="c1",
                                              client_secret_enc=SecretStr("shh-plain"), registered_at=NOW))
    with pytest.raises(ValueError, match="code_verifier_enc"):
        await store.put_pending(pending_row(store, code_verifier_enc=SecretStr("verifier-plain")))
    assert await store.get_client("kb") is None


async def test_a_patch_with_plaintext_is_refused_and_changes_nothing(store):
    row = await store.replace_tokens(token_row(store))
    guard = {"version": [raw_generation(row, "version")]}
    with pytest.raises(ValueError, match="access_token_enc"):
        await store.patch_tokens("kb", {"access_token_enc": "tok-plain-by-mistake"}, where=guard)
    with pytest.raises(ValueError, match="refresh_token_enc"):
        await store.patch_tokens("kb", {"refresh_token_enc": SecretStr("ref-plain-by-mistake")}, where=guard)
    assert store.unseal((await store.get_tokens("kb")).access_token_enc) == "tok-plain"


async def test_the_refusal_names_the_field_and_never_the_value(store):
    with pytest.raises(ValueError) as caught:
        await store.replace_tokens(token_row(store, access_token_enc=SecretStr("tok-plain-by-mistake")))
    assert "tok-plain-by-mistake" not in str(caught.value)


async def test_a_signed_out_row_and_a_patch_that_clears_a_token_are_not_plaintext(store):
    row = await store.replace_tokens(token_row(store, access_token_enc=None, refresh_token_enc=None, state="needs_sign_in"))
    patched = await store.patch_tokens("kb", {"access_token_enc": None, "refresh_token_enc": None}, where={"version": [raw_generation(row, "version")]})
    assert patched is not None


# ---- the token row ----------------------------------------------------------------------------------------------------------------------------------------------------------


async def test_get_tokens_of_nothing_is_none(store):
    assert await store.get_tokens("kb") is None


async def test_replace_bumps_the_version(store):
    assert (await store.replace_tokens(token_row(store))).version == 1
    assert (await store.replace_tokens(token_row(store))).version == 2
    assert (await store.replace_tokens(token_row(store))).version == 3
    assert (await store.get_tokens("kb")).version == 3


async def test_replace_swaps_the_whole_row_and_clears_a_held_lease(store):
    """A new connect supersedes whatever was there: the tokens, the state and a refresh lease some pod still held."""
    leased = token_row(store, refresh_lease_until=NOW + timedelta(seconds=30), refresh_lease_owner="pod-a", state="error", reason="down")
    await store.replace_tokens(leased)
    await store.replace_tokens(token_row(store, access_token_enc=store.seal("tok-two"), scope="other"))
    row = await store.get_tokens("kb")
    assert store.unseal(row.access_token_enc) == "tok-two" and row.scope == "other"
    assert (row.state, row.reason, row.refresh_lease_until, row.refresh_lease_owner) == ("connected", None, None, None)


async def test_replace_keeps_each_toolset_apart(store):
    await store.replace_tokens(token_row(store, "kb"))
    await store.replace_tokens(token_row(store, "other", access_token_enc=store.seal("tok-other")))
    assert store.unseal((await store.get_tokens("kb")).access_token_enc) == "tok-plain"
    assert store.unseal((await store.get_tokens("other")).access_token_enc) == "tok-other"


async def test_a_create_that_loses_a_race_retries_as_an_update(store, monkeypatch):
    """Two connects at once (a double click): both read "no row"; the loser's create conflicts and it replaces the winner's row instead of failing."""
    await store.replace_tokens(token_row(store))
    real_get = store._tokens.get
    reads = []

    async def blind_first_read(id, **kwargs):  # noqa: A002
        reads.append(id)
        return None if len(reads) == 1 else await real_get(id, **kwargs)

    monkeypatch.setattr(store._tokens, "get", blind_first_read)
    replaced = await store.replace_tokens(token_row(store, access_token_enc=store.seal("tok-two")))
    assert replaced.version == 2 and store.unseal(replaced.access_token_enc) == "tok-two"
    assert len(reads) >= 2


async def test_concurrent_connects_each_get_their_own_version(store):
    """The replace is itself a compare-and-set on the version, so two connects racing never write the same version over each other."""
    await store.replace_tokens(token_row(store))
    results = await asyncio.gather(*(store.replace_tokens(token_row(store, access_token_enc=store.seal(f"tok-{i}"))) for i in range(6)))
    assert sorted(r.version for r in results) == [2, 3, 4, 5, 6, 7]
    assert (await store.get_tokens("kb")).version == 7


async def test_patch_tokens_is_a_compare_and_set(store):
    row = await store.replace_tokens(token_row(store))
    stale = await store.patch_tokens("kb", {"state": "error", "reason": "down"}, where={"version": [row.version + 5]})
    assert stale is None
    assert (await store.get_tokens("kb")).state == "connected"

    patched = await store.patch_tokens("kb", {"state": "error", "reason": "down"}, where={"version": [raw_generation(row, "version")]})
    assert patched is not None and (patched.state, patched.reason) == ("error", "down")
    assert (await store.get_tokens("kb")).state == "error"


async def test_patch_tokens_on_a_missing_row_is_not_applied(store):
    assert await store.patch_tokens("nobody", {"state": "error"}, where={"version": [1]}) is None


async def test_patch_tokens_unwraps_secret_values_and_keeps_the_rest(store):
    row = await store.replace_tokens(token_row(store))
    patched = await store.patch_tokens(
        "kb", {"access_token_enc": store.seal("tok-two"), "version": 2}, where={"version": [raw_generation(row, "version")]},
    )
    assert store.unseal(patched.access_token_enc) == "tok-two"
    assert store.unseal(patched.refresh_token_enc) == "ref-plain", "a field the patch did not name is untouched"
    assert "tok-two" not in json.dumps(patched.model_dump(mode="json"))


async def test_patch_tokens_can_sign_a_toolset_out(store):
    row = await store.replace_tokens(token_row(store))
    patched = await store.patch_tokens(
        "kb", {"access_token_enc": None, "refresh_token_enc": None, "state": "needs_sign_in", "reason": "Not connected."},
        where={"version": [raw_generation(row, "version")]},
    )
    assert (patched.access_token_enc, patched.refresh_token_enc, patched.state) == (None, None, "needs_sign_in")


async def test_a_patched_timestamp_is_in_the_form_a_later_guard_compares(store):
    """The refresh lease is a timestamp guarded by compare-and-set: what a patch writes must read back in the form ``raw_generation`` gives, or the next guard never matches."""
    row = await store.replace_tokens(token_row(store))
    until = NOW + timedelta(seconds=30, microseconds=123456)
    leased = await store.patch_tokens(
        "kb", {"refresh_lease_until": until, "refresh_lease_owner": "pod-a"},
        where={"version": [raw_generation(row, "version")], "refresh_lease_until": [raw_generation(row, "refresh_lease_until")]},
    )
    assert leased is not None and leased.refresh_lease_until == until

    released = await store.patch_tokens(
        "kb", {"refresh_lease_until": None, "refresh_lease_owner": None},
        where={"refresh_lease_until": [raw_generation(leased, "refresh_lease_until")], "refresh_lease_owner": ["pod-a"]},
    )
    assert released is not None and released.refresh_lease_until is None


async def test_only_one_of_two_racing_lease_takers_wins(store):
    """The cross-pod refresh lease in one line: the same guarded patch from two callers, exactly one applies."""
    row = await store.replace_tokens(token_row(store))
    guard = {"version": [raw_generation(row, "version")], "refresh_lease_until": [raw_generation(row, "refresh_lease_until")]}
    results = await asyncio.gather(*(
        store.patch_tokens("kb", {"refresh_lease_until": NOW + timedelta(seconds=30), "refresh_lease_owner": f"pod-{i}"}, where=guard) for i in range(5)
    ))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert (await store.get_tokens("kb")).refresh_lease_owner == winners[0].refresh_lease_owner


async def test_delete_tokens_is_idempotent(store):
    await store.replace_tokens(token_row(store))
    assert await store.delete_tokens("kb") is True
    assert await store.delete_tokens("kb") is False
    assert await store.get_tokens("kb") is None


# ---- the registered client --------------------------------------------------------------------------------------------------------------------------------------------------


async def test_put_client_replaces_the_registration(store):
    assert await store.get_client("kb") is None
    first = McpOAuthClient(id="kb", issuer="https://as.example", redirect_uri="https://p.example/cb", client_id="c1", registered_at=NOW)
    await store.put_client(first)
    assert (await store.get_client("kb")).client_id == "c1"
    second = McpOAuthClient(id="kb", issuer="https://as.example", redirect_uri="https://q.example/cb", client_id="c2",
                            client_secret_enc=store.seal("shh"), registered_at=NOW)
    assert (await store.put_client(second)).client_id == "c2"
    got = await store.get_client("kb")
    assert (got.client_id, got.redirect_uri) == ("c2", "https://q.example/cb") and store.unseal(got.client_secret_enc) == "shh"


# ---- pending sign-ins -------------------------------------------------------------------------------------------------------------------------------------------------------


async def test_take_pending_is_single_use(store):
    await store.put_pending(pending_row(store))
    taken = await store.take_pending("state-1", now=NOW)
    assert taken is not None and taken.id == pending_key("state-1") and taken.toolset_id == "kb"
    assert store.unseal(taken.code_verifier_enc) == "verifier-plain"
    assert await store.take_pending("state-1", now=NOW) is None


async def test_a_pending_sign_in_is_stored_under_a_hash_of_its_state(store, provider):
    """The state is a live capability for ten minutes, and storage code prints row ids (the drift tripwire of ``patch_if_checked`` logs the id it was
    given): the row is stored under the SHA-256 of its state, never the state, and only the state itself takes it back, not the stored key."""
    state = "st-" + "q" * 40
    await store.put_pending(pending_row(store, state))
    storage = provider.get_storage(McpOAuthPendingSignIn)
    assert pending_key(state) == hashlib.sha256(state.encode("utf-8")).hexdigest()
    assert await storage.get(state) is None
    stored = await storage.get(pending_key(state))
    assert stored is not None and state not in json.dumps(dump_for_storage(stored))
    assert await store.take_pending(pending_key(state), now=NOW) is None, "the stored key is not the capability"
    taken = await store.take_pending(state, now=NOW)
    assert taken is not None and taken.id == pending_key(state) and taken.toolset_id == "kb"


async def test_take_pending_removes_the_row(store, provider):
    await store.put_pending(pending_row(store))
    await store.take_pending("state-1", now=NOW)
    assert await provider.get_storage(McpOAuthPendingSignIn).get(pending_key("state-1")) is None


async def test_a_caller_that_read_the_row_before_a_claim_and_a_delete_gets_none(store, monkeypatch):
    """The loser of a race reads the row, the winner claims it and deletes it, and only then does the loser try to claim: the answer is "not yours", not an error.

    Found on a live Postgres, where the claim of a row that is gone is a NotFoundError; the fake and SQLite never interleave a read and a claim that tightly.
    """
    await store.put_pending(pending_row(store))
    stale = await store._pending.get(pending_key("state-1"))
    assert await store.take_pending("state-1", now=NOW) is not None          # the winner: claims and deletes

    async def read_before_the_claim(id, **kwargs):  # noqa: A002
        return stale

    monkeypatch.setattr(store._pending, "get", read_before_the_claim)
    assert await store.take_pending("state-1", now=NOW) is None


async def test_an_unknown_state_is_none(store):
    assert await store.take_pending("never-issued", now=NOW) is None


async def test_two_concurrent_takes_get_one_row(store):
    await store.put_pending(pending_row(store))
    results = await asyncio.gather(store.take_pending("state-1", now=NOW), store.take_pending("state-1", now=NOW))
    assert [r is not None for r in results].count(True) == 1


async def test_many_concurrent_takes_get_one_row(store):
    for n in range(5):
        await store.put_pending(pending_row(store, f"state-{n}"))
    takes = [store.take_pending(f"state-{n % 5}", now=NOW) for n in range(25)]
    results = await asyncio.gather(*takes)
    taken = [r.id for r in results if r is not None]
    assert sorted(taken) == sorted(pending_key(f"state-{n}") for n in range(5)), "every state was given out exactly once"


async def test_an_expired_pending_sign_in_is_refused_and_removed(store, provider):
    await store.put_pending(pending_row(store, expires_at=NOW + timedelta(minutes=10)))
    assert await store.take_pending("state-1", now=NOW + timedelta(minutes=10, seconds=1)) is None
    assert await provider.get_storage(McpOAuthPendingSignIn).get(pending_key("state-1")) is None


async def test_a_pending_sign_in_is_expired_at_its_expiry_instant(store):
    await store.put_pending(pending_row(store))
    assert await store.take_pending("state-1", now=NOW + timedelta(minutes=10)) is None


async def test_a_pending_sign_in_one_second_before_expiry_is_still_good(store):
    await store.put_pending(pending_row(store))
    assert await store.take_pending("state-1", now=NOW + timedelta(minutes=10) - timedelta(seconds=1)) is not None


async def test_a_consumed_pending_row_is_never_given_out(store):
    """A row left marked consumed (a crash between the claim and the delete) is as dead as a deleted one."""
    await store.put_pending(pending_row(store, consumed=True))
    assert await store.take_pending("state-1", now=NOW) is None


async def test_purge_pending_drops_only_expired_rows_of_one_toolset(store, provider):
    await store.put_pending(pending_row(store, "kb-old", "kb", expires_at=NOW - timedelta(minutes=1)))
    await store.put_pending(pending_row(store, "kb-live", "kb", expires_at=NOW + timedelta(minutes=5)))
    await store.put_pending(pending_row(store, "other-old", "other", expires_at=NOW - timedelta(minutes=1)))
    assert await store.purge_pending("kb", now=NOW) == 1
    storage = provider.get_storage(McpOAuthPendingSignIn)
    assert await storage.get(pending_key("kb-old")) is None
    assert await storage.get(pending_key("kb-live")) is not None
    assert await storage.get(pending_key("other-old")) is not None
    assert await store.purge_pending("kb", now=NOW) == 0


async def test_purge_pending_pages_through_many_rows(store, provider):
    for n in range(230):
        await store.put_pending(pending_row(store, f"old-{n}", "kb", expires_at=NOW - timedelta(minutes=1)))
    await store.put_pending(pending_row(store, "live", "kb"))
    assert await store.purge_pending("kb", now=NOW) == 230
    assert await provider.get_storage(McpOAuthPendingSignIn).get(pending_key("live")) is not None


async def test_forget_removes_tokens_client_and_pending_of_one_toolset(store, provider):
    for toolset in ("kb", "other"):
        await store.replace_tokens(token_row(store, toolset))
        await store.put_client(McpOAuthClient(id=toolset, issuer="https://as.example", redirect_uri="https://p.example/cb", client_id="c1", registered_at=NOW))
        await store.put_pending(pending_row(store, f"{toolset}-live", toolset))
        await store.put_pending(pending_row(store, f"{toolset}-old", toolset, expires_at=NOW - timedelta(minutes=1)))
    await store.forget("kb")
    assert await store.get_tokens("kb") is None and await store.get_client("kb") is None
    pending = provider.get_storage(McpOAuthPendingSignIn)
    assert await pending.get(pending_key("kb-live")) is None and await pending.get(pending_key("kb-old")) is None, "every pending row goes, live ones included"
    assert await store.get_tokens("other") is not None and await store.get_client("other") is not None
    assert await pending.get(pending_key("other-live")) is not None and await pending.get(pending_key("other-old")) is not None


async def test_forget_is_idempotent(store):
    await store.forget("kb")
    await store.forget("kb")
