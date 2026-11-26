"""Review 2026-11-23 6.11: the webhook HMAC signs the body only, so a captured signed request replays indefinitely.

The signed ``X-Primer-Timestamp`` (unix seconds) now covers the raw body:
``X-Primer-Signature: sha256=<hex>`` is the HMAC-SHA256 of ``"<ts>\\n<raw body>"``
keyed with the secret when the timestamp header is present. A timestamp
outside the 300 s window is refused (``timestamp_stale``), an exact replay of
a signed request inside the window is refused (``replayed``), and a
malformed timestamp is refused (``timestamp_invalid``). Body-only signatures
(the pre-timestamp scheme) stay accepted: existing senders keep working
(legacy mode; a trigger whose config carries ``legacy_body_hmac=False``
refuses them with ``timestamp_required``).
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from primer.api.routers import webhooks
from primer.model.trigger import WebhookTriggerConfig
from primer.trigger.service import ServiceDeps, create_trigger

_SECRET = "s3cret-replay"


def _sig(secret: str, body: bytes, ts: str | None = None) -> str:
    msg = f"{ts}\n".encode() + body if ts is not None else body
    return "sha256=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


async def _trigger(client, fake_storage_provider, slug: str, *, legacy: bool = False):
    from pydantic import SecretStr

    return await create_trigger(
        slug=slug, name="WH", description=None,
        config=WebhookTriggerConfig(hmac_secret=SecretStr(_SECRET), legacy_body_hmac=legacy), enabled=True,
        deps=ServiceDeps(storage_provider=fake_storage_provider),
    )


async def test_a_fresh_timestamped_signature_is_accepted(client, fake_storage_provider):
    trigger = await _trigger(client, fake_storage_provider, "wh-replay-fresh")
    body = b'{"event": "fresh", "n": 1}'
    ts = str(int(time.time()))
    r = await client.post(
        f"/v1/webhooks/{trigger.config.token}", content=body,
        headers={"x-primer-signature": _sig(_SECRET, body, ts),
                 "x-primer-timestamp": ts},
    )
    assert r.status_code == 202, r.text


async def test_an_exact_replay_of_a_signed_request_is_refused(client, fake_storage_provider):
    trigger = await _trigger(client, fake_storage_provider, "wh-replay-exact")
    body = b'{"event": "push", "n": 2}'
    ts = str(int(time.time()))
    sig = _sig(_SECRET, body, ts)
    first = await client.post(
        f"/v1/webhooks/{trigger.config.token}", content=body,
        headers={"x-primer-signature": sig, "x-primer-timestamp": ts},
    )
    assert first.status_code == 202, first.text

    # The captured request, byte for byte, again: refused as a replay.
    replay = await client.post(
        f"/v1/webhooks/{trigger.config.token}", content=body,
        headers={"x-primer-signature": sig, "x-primer-timestamp": ts},
    )
    assert replay.status_code == 401, replay.text
    assert replay.json()["extensions"]["code"] == "replayed"


async def test_a_stale_timestamp_is_refused(client, fake_storage_provider):
    trigger = await _trigger(client, fake_storage_provider, "wh-replay-stale")
    body = b'{"event": "stale", "n": 3}'
    ts = str(int(time.time()) - 600)  # 10 min old, outside the 300 s window
    r = await client.post(
        f"/v1/webhooks/{trigger.config.token}", content=body,
        headers={"x-primer-signature": _sig(_SECRET, body, ts),
                 "x-primer-timestamp": ts},
    )
    assert r.status_code == 401, r.text
    assert r.json()["extensions"]["code"] == "timestamp_stale"


async def test_a_malformed_timestamp_is_refused(client, fake_storage_provider):
    trigger = await _trigger(client, fake_storage_provider, "wh-replay-badts")
    body = b'{"event": "badts", "n": 4}'
    r = await client.post(
        f"/v1/webhooks/{trigger.config.token}", content=body,
        headers={"x-primer-signature": _sig(_SECRET, body, "not-a-number"),
                 "x-primer-timestamp": "not-a-number"},
    )
    assert r.status_code == 401, r.text
    assert r.json()["extensions"]["code"] == "timestamp_invalid"


async def test_a_wrong_signature_with_a_valid_timestamp_is_refused(client, fake_storage_provider):
    trigger = await _trigger(client, fake_storage_provider, "wh-replay-wrong")
    body = b'{"event": "wrong", "n": 5}'
    ts = str(int(time.time()))
    r = await client.post(
        f"/v1/webhooks/{trigger.config.token}", content=body,
        headers={"x-primer-signature": _sig("other-secret", body, ts),
                 "x-primer-timestamp": ts},
    )
    assert r.status_code == 401, r.text
    assert r.json()["extensions"]["code"] == "hmac_mismatch"


async def test_a_body_only_signature_is_still_accepted_as_legacy(client, fake_storage_provider):
    """The pre-timestamp scheme keeps working, so existing senders are not broken."""
    trigger = await _trigger(client, fake_storage_provider, "wh-replay-legacy", legacy=True)
    body = b'{"event": "legacy", "n": 6}'
    r = await client.post(
        f"/v1/webhooks/{trigger.config.token}", content=body,
        headers={"x-primer-signature": _sig(_SECRET, body)},
    )
    assert r.status_code == 202, r.text


# ---------------------------------------------------------------------------
# The verifier itself (unit level, like the W46 tests)
# ---------------------------------------------------------------------------


def test_the_verifier_replays_only_inside_the_window_for_a_known_signature():
    body = b'{"a": 1}'
    ts = str(int(time.time()))
    sig = _sig("s3cret", body, ts).removeprefix("sha256=")

    ok, code = webhooks._verify_webhook_signature(
        "s3cret", body, _sig("s3cret", body, ts), ts,
        trigger_id="tr-a", legacy_body_only=True,
    )
    assert (ok, code) == (True, None)

    # The same captured signature again within the window: a replay.
    ok, code = webhooks._verify_webhook_signature(
        "s3cret", body, sig, ts, trigger_id="tr-a", legacy_body_only=True,
    )
    assert ok is False and code == "replayed"

    # A DIFFERENT trigger with the same secret and body is not a replay of
    # this one: the cache is keyed per trigger.
    ok, code = webhooks._verify_webhook_signature(
        "s3cret", body, sig, ts, trigger_id="tr-b", legacy_body_only=True,
    )
    assert ok is True, code


def test_the_verifier_refuses_a_stale_or_malformed_timestamp():
    body = b'{"a": 1}'
    stale = str(int(time.time()) - 600)
    ok, code = webhooks._verify_webhook_signature(
        "s3cret", body, _sig("s3cret", body, stale), stale,
        trigger_id="tr-c", legacy_body_only=True,
    )
    assert ok is False and code == "timestamp_stale"
    ok, code = webhooks._verify_webhook_signature(
        "s3cret", body, _sig("s3cret", body, "nope"), "nope",
        trigger_id="tr-c", legacy_body_only=True,
    )
    assert ok is False and code == "timestamp_invalid"


def test_the_verifier_still_accepts_body_only_and_refuses_when_legacy_is_off():
    body = b'{"a": 1}'
    ok, code = webhooks._verify_webhook_signature(
        "s3cret", body, _sig("s3cret", body), None,
        trigger_id="tr-d", legacy_body_only=True,
    )
    assert (ok, code) == (True, None)

    # A trigger that disallows legacy body-only signatures (config flag
    # landed) refuses the same request with its own code.
    ok, code = webhooks._verify_webhook_signature(
        "s3cret", body, _sig("s3cret", body), None,
        trigger_id="tr-e", legacy_body_only=False,
    )
    assert ok is False and code == "timestamp_required"

    # A non-ASCII signature is a mismatch, never an error (W46, kept).
    ok, code = webhooks._verify_webhook_signature(
        "s3cret", b"{}", "sha256=" + "\xff" * 4, None,
        trigger_id="tr-f", legacy_body_only=True,
    )
    assert ok is False and code == "hmac_mismatch"


# ---------------------------------------------------------------------------
# Round 1 (review 2026-11-23 lane J): C1, I1, I2, M4
# ---------------------------------------------------------------------------


def test_the_third_and_fourth_submissions_of_a_capture_are_refused_too():
    """C1: the replay lookup POPPED the entry, so a refusal consumed it and
    the next copy found no entry and was accepted (accepted, replayed,
    accepted, replayed). The unexpired entry must survive the refusal."""
    webhooks._replayed_signatures.clear()
    body = b'{"c1": 1}'
    ts = str(int(time.time()))
    sig = _sig("s3cret", body, ts)
    results = [
        webhooks._verify_webhook_signature(
            "s3cret", body, sig, ts, trigger_id="tr-c1", legacy_body_only=True,
        )
        for _ in range(4)
    ]
    assert results[0] == (True, None)
    assert results[1] == (False, "replayed")
    assert results[2] == (False, "replayed"), results[2]
    assert results[3] == (False, "replayed"), results[3]


def test_a_future_dated_replay_stays_refused_while_the_timestamp_is_valid(monkeypatch):
    """I1: the cache entry expired at acceptance + 300 s, but a timestamp
    290 s in the FUTURE stays valid (wall clock) until int(ts) + 300 s - so
    the replay was re-accepted ~301 s after acceptance. The entry must live
    as long as the timestamp itself."""
    webhooks._replayed_signatures.clear()
    body = b'{"i1": 1}'
    now_wall = time.time()
    ts = str(int(now_wall) + 290)  # sender clock ~5 min fast, still inside the window
    sig = _sig("s3cret", body, ts)
    wall = [now_wall]
    mono = [now_wall]
    monkeypatch.setattr(webhooks.time, "time", lambda: wall[0])
    monkeypatch.setattr(webhooks.time, "monotonic", lambda: mono[0])
    ok, code = webhooks._verify_webhook_signature(
        "s3cret", body, sig, ts, trigger_id="tr-i1", legacy_body_only=True,
    )
    assert (ok, code) == (True, None)
    wall[0] += 301
    mono[0] += 301  # ts - now = -11 s: the timestamp is still inside the window
    ok, code = webhooks._verify_webhook_signature(
        "s3cret", body, sig, ts, trigger_id="tr-i1", legacy_body_only=True,
    )
    assert (ok, code) == (False, "replayed"), (
        "the replay was accepted after the cache entry expired"
    )


@pytest.mark.parametrize("ts", ["²", "9" * 400, "9" * 5000])
def test_a_malformed_timestamp_is_refused_not_raised(ts):
    """I2: str.isdigit() alone lets "²" (int() raises) and 400/5000-digit
    strings (OverflowError / the int digit limit) through, which answered 500
    with a logged traceback. Strict parse: ascii digits, at most 12."""
    body = b'{"i2": 1}'
    try:
        ok, code = webhooks._verify_webhook_signature(
            "s3cret", body, _sig("s3cret", body, ts), ts,
            trigger_id="tr-i2", legacy_body_only=True,
        )
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{ts[:8]!r}... raised {type(exc).__name__}: {exc}")
    assert (ok, code) == (False, "timestamp_invalid"), (ts[:8], ok, code)


async def test_huge_timestamps_are_refused_401_not_500(client, fake_storage_provider):
    """I2, endpoint level: the huge digit strings must answer 401
    timestamp_invalid, not a 500 raised through the ASGI transport."""
    trigger = await _trigger(client, fake_storage_provider, "wh-replay-huge")
    body = b'{"event": "huge", "n": 7}'
    for ts in ("9" * 400, "9" * 5000):
        r = await client.post(
            f"/v1/webhooks/{trigger.config.token}", content=body,
            headers={"x-primer-signature": _sig(_SECRET, body, ts),
                     "x-primer-timestamp": ts},
        )
        assert r.status_code == 401, (len(ts), r.status_code, r.text)
        assert r.json()["extensions"]["code"] == "timestamp_invalid"


def test_one_trigger_cannot_evict_another_trigger_replay_entry(monkeypatch):
    """M4: the cache was one global bound evicted by insertion order, so a
    user with many triggers flooding the cache pushed another trigger's
    still-valid entry out. Evict expired entries first, then trim the
    trigger holding the most entries."""
    webhooks._replayed_signatures.clear()
    monkeypatch.setattr(webhooks, "_REPLAY_CACHE_MAX_KEYS", 100)  # same mechanism, small bound
    body = b'{"m4": 1}'
    ts = str(int(time.time()))
    victim_sig = _sig("s3cret", body, ts)
    ok, code = webhooks._verify_webhook_signature(
        "s3cret", body, victim_sig, ts, trigger_id="victim", legacy_body_only=True,
    )
    assert (ok, code) == (True, None)
    # An attacker with many triggers then floods the cache past the bound
    # (distinct bodies: a repeated body+timestamp is refused replayed and
    # never adds an entry, so the cache would never fill).
    for i in range(101):
        attacker_body = f'{{"i": {i}}}'.encode()
        webhooks._verify_webhook_signature(
            "s3cret", attacker_body, _sig("s3cret", attacker_body, ts), ts,
            trigger_id=f"attacker-{i % 34}", legacy_body_only=True,
        )
    ok, code = webhooks._verify_webhook_signature(
        "s3cret", body, victim_sig, ts, trigger_id="victim", legacy_body_only=True,
    )
    assert (ok, code) == (False, "replayed"), (
        "the victim's entry was evicted inside its window"
    )
