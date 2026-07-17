"""Tests for the triggers REST router — Spec §10, Plan Phase 7.

Covers the public scenarios called out in the Phase 7 plan:

* POST /v1/triggers with kind=delayed  → 201
* POST /v1/triggers with kind=scheduled → 201
* POST with invalid cron               → 422 with detail.code = "cron_invalid"
* POST with invalid timezone           → 422 with detail.code = "timezone_invalid"
* POST with duplicate slug             → 409 with detail.code = "trigger_slug_conflict"
* POST subscription with kind=parked_session → 422 with
  detail.code = "parked_session_only_from_yield"
* GET list filter by kind
* PUT with kind change → 409 with detail.code = "trigger_kind_immutable"
* DELETE cascades subscriptions
* POST /v1/triggers/{id}/fire_now → 200 with body containing fire_id + results
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _future_iso(seconds: int = 3600) -> str:
    """Return an ISO 8601 UTC timestamp ``seconds`` in the future."""
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat()


def _delayed_body(
    slug: str = "one-off",
    name: str = "One-off",
    *,
    fire_at: str | None = None,
    enabled: bool = True,
) -> dict:
    return {
        "slug": slug,
        "name": name,
        "config": {
            "kind": "delayed",
            "fire_at": fire_at or _future_iso(),
        },
        "enabled": enabled,
    }


def _scheduled_body(
    slug: str = "every-day",
    name: str = "Every Day",
    *,
    cron: str = "0 9 * * *",
    tz: str = "UTC",
    catchup: str = "one",
) -> dict:
    return {
        "slug": slug,
        "name": name,
        "config": {
            "kind": "scheduled",
            "cron": cron,
            "timezone": tz,
            "catchup": catchup,
        },
    }


def _detail_code(body: dict) -> str | None:
    """Pull the error code out of the RFC 7807 envelope.

    Routers raise ``HTTPException(detail={"code": ...})``; the problem+json
    handler preserves those keys verbatim under ``extensions``.
    """
    extensions = body.get("extensions")
    if isinstance(extensions, dict):
        return extensions.get("code")
    return None


# ---------------------------------------------------------------------------
# Create — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_delayed_trigger_returns_201(client):
    body = _delayed_body(slug="d-one", name="Delayed One")
    r = await client.post("/v1/triggers", json=body)
    assert r.status_code == 201, r.text
    payload = r.json()
    assert payload["slug"] == "d-one"
    assert payload["name"] == "Delayed One"
    assert payload["id"].startswith("tr-")
    assert payload["config"]["kind"] == "delayed"
    assert payload["enabled"] is True
    # Pending delayed fire — next_fire_at is the configured fire_at.
    assert payload["next_fire_at"] is not None


@pytest.mark.asyncio
async def test_create_scheduled_trigger_returns_201(client):
    body = _scheduled_body(slug="s-daily", name="Daily")
    r = await client.post("/v1/triggers", json=body)
    assert r.status_code == 201, r.text
    payload = r.json()
    assert payload["config"]["kind"] == "scheduled"
    assert payload["config"]["cron"] == "0 9 * * *"
    assert payload["config"]["timezone"] == "UTC"
    assert payload["next_fire_at"] is not None


# ---------------------------------------------------------------------------
# Create — validation errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_scheduled_invalid_cron_returns_422(client):
    body = _scheduled_body(slug="bad-cron", cron="not a cron")
    r = await client.post("/v1/triggers", json=body)
    assert r.status_code == 422, r.text
    assert _detail_code(r.json()) == "cron_invalid"


@pytest.mark.asyncio
async def test_create_scheduled_invalid_timezone_returns_422(client):
    body = _scheduled_body(slug="bad-tz", tz="Not/A_Zone")
    r = await client.post("/v1/triggers", json=body)
    assert r.status_code == 422, r.text
    assert _detail_code(r.json()) == "timezone_invalid"


@pytest.mark.asyncio
async def test_create_duplicate_slug_returns_409(client):
    body = _delayed_body(slug="dup-slug", name="First")
    r1 = await client.post("/v1/triggers", json=body)
    assert r1.status_code == 201, r1.text

    body2 = _delayed_body(slug="dup-slug", name="Second")
    r2 = await client.post("/v1/triggers", json=body2)
    assert r2.status_code == 409, r2.text
    assert _detail_code(r2.json()) == "trigger_slug_conflict"


# ---------------------------------------------------------------------------
# Subscriptions — parked_session guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_subscription_parked_session_rejected_422(client):
    # Need a parent trigger first.
    r = await client.post(
        "/v1/triggers",
        json=_delayed_body(slug="parent-parked", name="Parent"),
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]

    sub_body = {
        "config": {
            "kind": "parked_session",
            "session_id": "se-1",
            "tool_call_id": "tc-1",
            "parked_at": _future_iso(0),
        },
    }
    r2 = await client.post(f"/v1/triggers/{tid}/subscriptions", json=sub_body)
    assert r2.status_code == 422, r2.text
    assert _detail_code(r2.json()) == "parked_session_only_from_yield"


# ---------------------------------------------------------------------------
# List filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_triggers_filter_by_kind(client):
    # Seed one of each kind.
    r_d = await client.post(
        "/v1/triggers",
        json=_delayed_body(slug="filter-d", name="D"),
    )
    assert r_d.status_code == 201, r_d.text
    r_s = await client.post(
        "/v1/triggers",
        json=_scheduled_body(slug="filter-s", name="S"),
    )
    assert r_s.status_code == 201, r_s.text

    # Filter by kind=delayed.
    r_list = await client.get("/v1/triggers", params={"kind": "delayed"})
    assert r_list.status_code == 200, r_list.text
    items = r_list.json()["items"]
    kinds = {item["config"]["kind"] for item in items}
    assert "delayed" in kinds
    assert "scheduled" not in kinds
    slugs = {item["slug"] for item in items}
    assert "filter-d" in slugs
    assert "filter-s" not in slugs

    # Filter by kind=scheduled.
    r_list_s = await client.get("/v1/triggers", params={"kind": "scheduled"})
    assert r_list_s.status_code == 200, r_list_s.text
    items_s = r_list_s.json()["items"]
    slugs_s = {item["slug"] for item in items_s}
    assert "filter-s" in slugs_s
    assert "filter-d" not in slugs_s


# ---------------------------------------------------------------------------
# Update — kind immutable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_changing_kind_returns_409(client):
    # Create a delayed trigger.
    r = await client.post(
        "/v1/triggers",
        json=_delayed_body(slug="immut-kind", name="K"),
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]

    # Attempt to swap to scheduled — should hit the kind-immutable guard.
    update_body = {
        "config": {
            "kind": "scheduled",
            "cron": "0 9 * * *",
            "timezone": "UTC",
            "catchup": "one",
        },
    }
    r2 = await client.put(f"/v1/triggers/{tid}", json=update_body)
    assert r2.status_code == 409, r2.text
    assert _detail_code(r2.json()) == "trigger_kind_immutable"


# ---------------------------------------------------------------------------
# Delete cascades subscriptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_cascades_subscriptions(client, fake_storage_provider):
    from primer.model.trigger import Subscription

    # Parent trigger
    r = await client.post(
        "/v1/triggers",
        json=_delayed_body(slug="cascade-parent", name="P"),
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]

    # Create a chat_message subscription
    sub_body = {
        "config": {"kind": "chat_message", "chat_id": "ch-1"},
        "payload_template": None,
        "parallelism": "skip",
    }
    r_sub = await client.post(
        f"/v1/triggers/{tid}/subscriptions", json=sub_body,
    )
    assert r_sub.status_code == 201, r_sub.text
    sub_id = r_sub.json()["id"]

    # Confirm sub is listed pre-delete.
    r_list = await client.get(f"/v1/triggers/{tid}/subscriptions")
    assert r_list.status_code == 200, r_list.text
    assert any(s["id"] == sub_id for s in r_list.json()["items"])

    # Delete trigger — cascades subscriptions.
    r_del = await client.delete(f"/v1/triggers/{tid}")
    assert r_del.status_code == 204, r_del.text

    # Trigger gone.
    r_get = await client.get(f"/v1/triggers/{tid}")
    assert r_get.status_code == 404

    # Subscription gone from storage (verify directly).
    subs_storage = fake_storage_provider.get_storage(Subscription)
    leftover = await subs_storage.get(sub_id)
    assert leftover is None


# ---------------------------------------------------------------------------
# fire_now — returns fire_id + results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_now_returns_fire_id_and_results(client):
    # Seed a delayed trigger.
    r = await client.post(
        "/v1/triggers",
        json=_delayed_body(slug="fire-now", name="FN"),
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]

    r_fire = await client.post(f"/v1/triggers/{tid}/fire_now")
    assert r_fire.status_code == 200, r_fire.text
    body = r_fire.json()
    assert "fire_id" in body
    assert "results" in body
    # No subscriptions wired → empty results list, skipped flag False.
    assert body["skipped"] is False
    assert body["results"] == []
    assert isinstance(body["fire_id"], str)
    assert body["fire_id"]  # non-empty


@pytest.mark.asyncio
async def test_fire_now_disabled_trigger_returns_skipped(client):
    # Disabled triggers skip dispatch and report skipped=True without a fire_id.
    r = await client.post(
        "/v1/triggers",
        json=_delayed_body(slug="fire-disabled", name="D", enabled=False),
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]

    r_fire = await client.post(f"/v1/triggers/{tid}/fire_now")
    assert r_fire.status_code == 200, r_fire.text
    body = r_fire.json()
    assert body["skipped"] is True
    assert body["fire_id"] is None


@pytest.mark.asyncio
async def test_fire_now_missing_trigger_returns_404(client):
    r = await client.post("/v1/triggers/tr-doesnotexist/fire_now")
    assert r.status_code == 404
    assert _detail_code(r.json()) == "trigger_not_found"


# ---------------------------------------------------------------------------
# Extras — round-trip GET, 404, list returns empty initially
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_triggers_empty_initially(client):
    r = await client.get("/v1/triggers")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_get_trigger_not_found_returns_404(client):
    r = await client.get("/v1/triggers/tr-missing-zzz")
    assert r.status_code == 404
    assert _detail_code(r.json()) == "trigger_not_found"
