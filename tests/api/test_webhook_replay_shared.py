"""X2.1: the webhook replay cache is shared across replicas.

The endpoint's in-memory dict is the first-level check (cheap, per process);
the shared store is the cross-replica arbiter: an accepted timestamped
signature inserts a row whose id is derived from (trigger id, canonical
signature hex), so a second replica's insert of the same key conflicts on
every backend and is refused ``replayed`` (the same conditional-insert
pattern as ``claim_thread``). Expired rows (the signature's own timestamp
plus the 5-minute window) are pruned lazily on the next accept.

"Two replicas" here means two independent accepts over the same store with
separate in-memory caches (two processes in one test), or two storage
provider instances over one SQLite file (two real connections). The live
Postgres twin is ``tests/storage/test_conditional_claim_insert.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as _hmac
import time
from datetime import datetime, timedelta, timezone

import pytest

from primer.api.routers import webhooks
from primer.model.webhook_signature import WebhookSignature
from tests.conftest import _FakeStorageProvider

SECRET = "s3cret"
BODY = b'{"event": "push"}'
TS = str(int(time.time()))


@pytest.fixture(autouse=True)
def _fresh_ts():
    """Take the timestamp when each test runs. One taken at import goes stale (past the 300 s window) when a
    long CI shard reaches this module many minutes after collection, and a stale timestamp is refused and its
    row already expired."""
    global TS
    TS = str(int(time.time()))


def _sig(secret: str, body: bytes, ts: str) -> str:
    return "sha256=" + _hmac.new(
        secret.encode(), f"{ts}\n".encode() + body, hashlib.sha256,
    ).hexdigest()


def _sig_body_only(secret: str, body: bytes) -> str:
    """The pre-timestamp (legacy) scheme: the MAC of the raw body only."""
    return "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_a_replay_is_refused_by_the_store_when_the_in_memory_cache_is_cold(
    monkeypatch,
):
    """Two replicas (two in-memory caches) over one store: replica B's
    verify passes on its cold cache, and the refusal must come from the
    store's conditional insert, not the in-memory dict."""
    from primer.api.routers.webhooks import _claim_signature

    sp = _FakeStorageProvider()
    # Replica A accepts the request: the in-memory check passes and the
    # shared store records the signature.
    ok, code = webhooks._verify_webhook_signature(
        SECRET, BODY, _sig(SECRET, BODY, TS), TS,
        trigger_id="tr-1", legacy_body_only=False,
    )
    assert (ok, code) == (True, None)
    assert await _claim_signature(sp, "tr-1", SECRET, BODY, TS) is True

    # Replica B receives the captured request: its in-memory cache is cold,
    # so its verify passes - the store must be what refuses it.
    monkeypatch.setattr(webhooks, "_replayed_signatures", {})
    ok, code = webhooks._verify_webhook_signature(
        SECRET, BODY, _sig(SECRET, BODY, TS), TS,
        trigger_id="tr-1", legacy_body_only=False,
    )
    assert ok is True, "a cold in-memory cache passes the first-level check"
    assert await _claim_signature(sp, "tr-1", SECRET, BODY, TS) is False, (
        "the duplicate (trigger, signature) insert must conflict with the "
        "row replica A already committed"
    )


@pytest.mark.asyncio
async def test_the_first_accept_inserts_and_a_second_accept_of_the_same_key_refuses(
    monkeypatch,
):
    from primer.api.routers.webhooks import _claim_signature

    sp = _FakeStorageProvider()
    assert await _claim_signature(sp, "tr-2", SECRET, BODY, TS) is True
    assert await _claim_signature(sp, "tr-2", SECRET, BODY, TS) is False, (
        "an exact resubmission inside the window is refused `replayed`"
    )
    # A different signature (a different body) is a different event: accepted.
    assert await _claim_signature(sp, "tr-2", SECRET, BODY + b"x", TS) is True


@pytest.mark.asyncio
async def test_expired_entries_are_pruned_on_the_next_accept(monkeypatch):
    from primer.api.routers.webhooks import _claim_signature

    sp = _FakeStorageProvider()
    storage = sp.get_storage(WebhookSignature)
    old_id = "webhook-signature-" + hashlib.sha256(b"tr-3\0oldhex").hexdigest()[:32]
    await storage.create(WebhookSignature(
        id=old_id, trigger_id="tr-3", signature="oldhex",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    ))

    # A different body -> a different signature hex -> a different row id, so
    # the prune (not a conflicting insert) is what removes the old row.
    assert await _claim_signature(sp, "tr-3", SECRET, BODY, TS) is True
    assert await storage.get(old_id) is None, "the expired entry must be pruned"


@pytest.mark.asyncio
async def test_the_prune_is_bounded_per_accept():
    """F3: a burst after a quiet period must not add an unbounded number of
    round trips to one request's latency: one accept deletes at most the
    bound of expired rows; the rest wait for the next accept."""
    from primer.api.routers.webhooks import _claim_signature

    sp = _FakeStorageProvider()
    storage = sp.get_storage(WebhookSignature)
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    # The bound (the implementation's _PRUNE_MAX_PER_REQUEST) plus a backlog
    # beyond it.
    backlog = 100 + 5
    ids = []
    for i in range(backlog):
        row_id = f"webhook-signature-prune-{i:03d}"
        await storage.create(WebhookSignature(
            id=row_id, trigger_id=f"tr-prune-{i}", signature=f"hex-{i}",
            expires_at=expired_at,
        ))
        ids.append(row_id)

    assert await _claim_signature(sp, "tr-prune-accept", SECRET, BODY, TS) is True

    remaining = [r for r in ids if await storage.get(r) is not None]
    assert len(remaining) == 5, (
        f"one accept pruned {backlog - len(remaining)} expired rows: the prune "
        "must stop after the bound per request (the rest go on the next accept)"
    )


@pytest.mark.asyncio
async def test_a_body_only_acceptance_never_touches_the_shared_store(
    client, fake_storage_provider,
):
    """Body-only (legacy) signatures carry no window and must keep working:
    they never touch the shared store. The earlier version of this test
    called only the verifier, which by design never touches the store, so it
    proved nothing about the endpoint's claim path: a body-only (legacy)
    signed request is driven THROUGH the endpoint instead, and the store
    must stay empty afterwards (a legitimate resend must keep working)."""
    from pydantic import SecretStr

    from primer.model.trigger import WebhookTriggerConfig
    from primer.trigger.service import ServiceDeps, create_trigger

    trigger = await create_trigger(
        slug="wh-legacy-store", name="WH", description=None,
        config=WebhookTriggerConfig(
            hmac_secret=SecretStr(SECRET), legacy_body_hmac=True,
        ),
        enabled=True,
        deps=ServiceDeps(storage_provider=fake_storage_provider),
    )
    body = b'{"event": "legacy", "n": 9}'
    r = await client.post(
        f"/v1/webhooks/{trigger.config.token}", content=body,
        headers={"x-primer-signature": _sig_body_only(SECRET, body)},
    )
    assert r.status_code == 202, r.text
    assert fake_storage_provider.get_storage(WebhookSignature)._data == {}, (
        "a body-only (legacy) acceptance recorded a shared-store row: it "
        "carries no window, so a legitimate resend would be refused"
    )


@pytest.mark.asyncio
async def test_a_timestamped_request_accepted_by_replica_a_is_refused_by_replica_b(
    client, fake_storage_provider, monkeypatch,
):
    """Endpoint-level proof of the cross-replica refusal: two app instances
    over the same storage. Replica A's accept records the signature in the
    shared store; replica B's in-memory cache is cold, so its first-level
    check passes and the refusal must come from the store's conditional
    insert - 401 ``replayed`` at the endpoint."""
    from pydantic import SecretStr

    import httpx
    from primer.api.app import create_test_app
    from primer.api.registries import ProviderRegistry
    from primer.model.trigger import WebhookTriggerConfig
    from primer.trigger.service import ServiceDeps, create_trigger

    trigger = await create_trigger(
        slug="wh-replica", name="WH", description=None,
        config=WebhookTriggerConfig(hmac_secret=SecretStr(SECRET)),
        enabled=True,
        deps=ServiceDeps(storage_provider=fake_storage_provider),
    )
    body = b'{"event": "replica"}'
    ts = str(int(time.time()))
    headers = {
        "x-primer-signature": _sig(SECRET, body, ts),
        "x-primer-timestamp": ts,
    }
    # Replica A accepts the request: its in-memory cache warms and the
    # shared store records the signature.
    first = await client.post(
        f"/v1/webhooks/{trigger.config.token}", content=body, headers=headers,
    )
    assert first.status_code == 202, first.text

    # Replica B: a second app instance over the same store. The in-memory
    # cache is the module-level dict (one per process), so a fresh replica
    # is modelled by clearing it: replica B's first-level check is cold and
    # only the shared store can refuse the captured request.
    monkeypatch.setattr(webhooks, "_replayed_signatures", {})
    registry = ProviderRegistry(
        fake_storage_provider,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),  # type: ignore[arg-type]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type]
    )
    app_b = create_test_app(
        storage_provider=fake_storage_provider,  # type: ignore[arg-type]
        provider_registry=registry,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_b), base_url="http://test",
    ) as client_b:
        replay = await client_b.post(
            f"/v1/webhooks/{trigger.config.token}", content=body, headers=headers,
        )
    assert replay.status_code == 401, replay.text
    assert replay.json()["extensions"]["code"] == "replayed", (
        "replica B's cold in-memory cache passed the captured request and "
        "the shared store did not refuse it"
    )


@pytest.mark.asyncio
async def test_two_replicas_over_one_sqlite_file_cannot_both_accept(tmp_path):
    """Two real storage connections (two replicas): exactly one accept of a
    captured (timestamp, signature) survives."""
    from primer.api.routers.webhooks import _claim_signature
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider

    path = str(tmp_path / "replay.sqlite")
    sp1 = SqliteStorageProvider(SqliteConfig(path=path))
    sp2 = SqliteStorageProvider(SqliteConfig(path=path))
    await sp1.initialize()
    await sp2.initialize()
    try:
        a, b = await asyncio.gather(
            _claim_signature(sp1, "tr-6", SECRET, BODY, TS),
            _claim_signature(sp2, "tr-6", SECRET, BODY, TS),
        )
        assert sorted([a, b]) == [False, True], (
            "exactly one replica may accept a captured signed request"
        )
    finally:
        await sp1.aclose()
        await sp2.aclose()
