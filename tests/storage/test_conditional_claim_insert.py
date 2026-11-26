"""Live-Postgres twin of the X2.1 conditional-claim tests: the skip gate's
cross-process claim (``primer/trigger/skip_claim.py``) and the webhook replay
cache's shared insert (``primer/api/routers/webhooks.py::_claim_signature``)
both ride on one guarantee - a conditional insert of a DERIVED id (two
claims conflict on the primary key, as ``claim_thread``'s do). Two storage
providers are two processes: separate pools, separate connections, one
database.

Gated on the single Postgres test gate (tests/pg_gate.py: PRIMER_TEST_POSTGRES_URL).
The claim/replay rows' ids are deterministic (derived from the key), so each
test starts by dropping its own row - otherwise a previous run's fresh row
would make every claim lose.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from urllib.parse import parse_qs, urlparse

import pytest

from primer.model.except_ import ConfigError
from primer.model.provider import PoolConfig, PostgresConfig
from primer.storage.postgres import PostgresStorageProvider
from tests.pg_gate import (
    CANONICAL_ENV,
    explicit_port,
    postgres_marks,
    require_postgres_url,
)

pytestmark = postgres_marks("the live conditional-claim tests")

_WHAT = "the live conditional-claim tests"


def _config() -> PostgresConfig:
    p = urlparse(require_postgres_url(_WHAT))
    if p.scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"unexpected scheme {p.scheme!r} in {CANONICAL_ENV}")
    schema = parse_qs(p.query).get("schema", ["public"])[0]
    return PostgresConfig(
        hostname=p.hostname or "localhost",
        port=explicit_port(p),
        username=p.username or "postgres",
        password=p.password or "",  # type: ignore[arg-type]
        database=(p.path or "/postgres").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=4),
    )


async def _providers():
    """Two initialised providers over the same database (two "processes")."""
    sp1 = PostgresStorageProvider(_config())
    sp2 = PostgresStorageProvider(_config())
    await sp1.initialize()
    await sp2.initialize()
    return sp1, sp2


async def _clean(sp, model, row_id: str) -> None:
    """Drop this test's derived row (deterministic ids collide across runs).
    Best effort: the lazy table DDL runs on this first storage operation, so
    a first-ever run starts clean by construction, and a missing row is a
    no-op."""
    with contextlib.suppress(Exception):
        await sp.get_storage(model).delete(row_id)


@pytest.mark.asyncio
async def test_two_providers_cannot_both_claim_one_subscription():
    from primer.model.trigger_skip_claim import TriggerSkipClaim
    from primer.trigger.skip_claim import (
        _claim_id,
        acquire_skip_claim,
        release_skip_claim,
    )

    sp1, sp2 = await _providers()
    try:
        sub_id, row_id = "sb-live-1", _claim_id("sb-live-1")
        await _clean(sp1, TriggerSkipClaim, row_id)

        a, b = await asyncio.gather(
            acquire_skip_claim(sp1, sub_id), acquire_skip_claim(sp2, sub_id),
        )
        assert (a is None) != (b is None), (
            "exactly one of two concurrent claims may win the conditional insert"
        )
        row = await sp1.get_storage(TriggerSkipClaim).get(row_id)
        assert row is not None, "the winner's claim row must be committed"
        for gen in (a, b):
            if gen is not None:
                await release_skip_claim(sp1, sub_id, gen)
    finally:
        await sp1.aclose()
        await sp2.aclose()


@pytest.mark.asyncio
async def test_an_expired_claim_is_taken_over_over_a_real_store():
    from datetime import datetime, timedelta, timezone

    from primer.model.trigger_skip_claim import TriggerSkipClaim
    from primer.trigger.skip_claim import _claim_id, acquire_skip_claim

    sp1, sp2 = await _providers()
    try:
        sub_id, row_id = "sb-live-2", _claim_id("sb-live-2")
        await _clean(sp1, TriggerSkipClaim, row_id)

        # A holder that crashed: the row is committed but past its TTL.
        storage = sp1.get_storage(TriggerSkipClaim)
        stale = datetime.now(timezone.utc)
        await storage.create(TriggerSkipClaim(
            id=row_id, subscription_id=sub_id,
            claimed_at=stale - timedelta(minutes=5),
            expires_at=stale - timedelta(minutes=4),
        ))

        assert await acquire_skip_claim(sp2, sub_id) is not None, (
            "after the TTL the next claim must take the stale row over"
        )
        assert await acquire_skip_claim(sp1, sub_id) is None, (
            "a live holder's claim must be refused, not stolen"
        )
    finally:
        await sp1.aclose()
        await sp2.aclose()


@pytest.mark.asyncio
async def test_two_takers_of_one_stale_claim_cannot_both_win():
    """F1 over a real store: two processes race one expired row; the guarded
    patch lets exactly one take it over."""
    from datetime import datetime, timedelta, timezone

    from primer.model.trigger_skip_claim import TriggerSkipClaim
    from primer.trigger.skip_claim import _claim_id, acquire_skip_claim

    sp1, sp2 = await _providers()
    try:
        sub_id, row_id = "sb-live-take-1", _claim_id("sb-live-take-1")
        await _clean(sp1, TriggerSkipClaim, row_id)

        # A holder that crashed: the row is committed but past its TTL.
        storage = sp1.get_storage(TriggerSkipClaim)
        stale = datetime.now(timezone.utc)
        await storage.create(TriggerSkipClaim(
            id=row_id, subscription_id=sub_id,
            claimed_at=stale - timedelta(minutes=5),
            expires_at=stale - timedelta(minutes=4),
        ))

        a, b = await asyncio.gather(
            acquire_skip_claim(sp1, sub_id), acquire_skip_claim(sp2, sub_id),
        )
        assert (a is None) != (b is None), (
            "two takers of one expired claim both won over a real store"
        )
    finally:
        await sp1.aclose()
        await sp2.aclose()


@pytest.mark.asyncio
async def test_a_stale_holder_releasing_cannot_delete_the_takers_claim():
    """F2 over a real store: the taker's claim survives the stale holder's
    late release; the taker's own release removes it."""
    from datetime import datetime, timedelta, timezone

    from primer.model.trigger_skip_claim import TriggerSkipClaim
    from primer.trigger.skip_claim import (
        _claim_id,
        acquire_skip_claim,
        release_skip_claim,
    )

    sp1, sp2 = await _providers()
    try:
        sub_id, row_id = "sb-live-take-2", _claim_id("sb-live-take-2")
        await _clean(sp1, TriggerSkipClaim, row_id)

        storage = sp1.get_storage(TriggerSkipClaim)
        stale = datetime.now(timezone.utc)
        stale_row = await storage.create(TriggerSkipClaim(
            id=row_id, subscription_id=sub_id,
            claimed_at=stale - timedelta(minutes=5),
            expires_at=stale - timedelta(minutes=4),
        ))

        taker_gen = await acquire_skip_claim(sp2, sub_id)
        assert taker_gen is not None, "the taker must take the stale row over"

        # The stale holder (process 1) releases late, with its own stamp.
        from primer.storage import raw_generation

        await release_skip_claim(sp1, sub_id, raw_generation(stale_row, "claimed_at"))

        row = await storage.get(row_id)
        assert row is not None, (
            "a stale holder's release deleted the taker's claim over a real store"
        )
        await release_skip_claim(sp2, sub_id, taker_gen)
        assert await storage.get(row_id) is None, "the taker's own release must remove the row"
    finally:
        await sp1.aclose()
        await sp2.aclose()


@pytest.mark.asyncio
async def test_two_replicas_cannot_both_accept_one_signature():
    from primer.api.routers.webhooks import _claim_signature
    from primer.model.webhook_signature import WebhookSignature

    sp1, sp2 = await _providers()
    try:
        # A current timestamp: the row expires with its own window (5
        # minutes out), so the second replica's prune does NOT remove the
        # first's row before its conflicting insert.
        trigger_id, secret, body, ts = (
            "tr-live-3", "s3cret", b'{"event": "push"}', str(int(time.time())),
        )
        row_id = "webhook-signature-" + _row_digest(trigger_id, secret, body, ts)
        await _clean(sp1, WebhookSignature, row_id)

        a, b = await asyncio.gather(
            _claim_signature(sp1, trigger_id, secret, body, ts),
            _claim_signature(sp2, trigger_id, secret, body, ts),
        )
        assert sorted([a, b]) == [False, True], (
            "exactly one replica may accept a captured signed request"
        )
    finally:
        await sp1.aclose()
        await sp2.aclose()


def _row_digest(trigger_id: str, secret: str, body: bytes, ts: str) -> str:
    """The row id ``_claim_signature`` derives (mirrors the implementation:
    sha256 of (trigger id, canonical expected hex), first 32 hex chars)."""
    import hashlib
    import hmac as _hmac

    expected = _hmac.new(
        secret.encode(), f"{ts}\n".encode() + body, hashlib.sha256,
    ).hexdigest()
    return hashlib.sha256(f"{trigger_id}\0{expected}".encode()).hexdigest()[:32]
