"""E2E test: ChannelProvider DELETE blocked while a Channel references it.

The §3 channels surface enforces a foreign-key style cascade-block on
the live Postgres backend: a ChannelProvider row cannot be deleted
while any Channel still names it as provider_id. The check fires in
matrix/api/routers/channels.py:_channel_provider_on_delete and raises
ConflictError (→ 409 /errors/conflict).

Covered backlog item:

* T0842 — Create provider + channel referencing it; DELETE provider
  returns 409 with detail mentioning the blocking channel id; DELETE
  channel succeeds; DELETE provider then succeeds.
"""

from __future__ import annotations

import json

import httpx
import pytest


# 60-char placeholder; satisfies DiscordChannelProviderConfig.bot_token
# length floor (>=30) without looking like a real token.
_FAKE_DISCORD_TOKEN = "x" * 60


# ===========================================================================
# T0842 — ChannelProvider DELETE → 409 while a Channel references it
# ===========================================================================


@pytest.mark.asyncio
async def test_t0842_channel_provider_delete_blocked_by_channel(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0842 — Pin the cascade-block on ChannelProvider DELETE over
    the live HTTP + Postgres stack (the tests/api/ sibling exercises
    the same path against the in-memory storage layer; this version
    confirms the production storage backend enforces it too).

    Sequence:
    1. POST /v1/channel_providers (discord, valid token) → 201
    2. POST /v1/channels referencing provider → 201
    3. DELETE provider → 409 /errors/conflict; detail names channel id
    4. DELETE channel → 200/204
    5. DELETE provider → 200/204 (now unblocked)
    """
    cp_id = f"cp-{unique_suffix}"
    ch_id = f"ch-{unique_suffix}"
    cleanup_urls = [f"/v1/channels/{ch_id}", f"/v1/channel_providers/{cp_id}"]
    try:
        # 1. Provider.
        r = await client.post(
            "/v1/channel_providers",
            json={
                "id": cp_id,
                "provider": "discord",
                "config": {"bot_token": _FAKE_DISCORD_TOKEN},
            },
        )
        assert r.status_code == 201, r.text

        # 2. Channel referencing it.
        r = await client.post(
            "/v1/channels",
            json={
                "id": ch_id,
                "provider_id": cp_id,
                "external_id": f"ext-{unique_suffix}",
                "label": "cascade probe",
            },
        )
        assert r.status_code == 201, r.text

        # 3. DELETE provider while child exists → 409.
        r = await client.delete(f"/v1/channel_providers/{cp_id}")
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["status"] == 409, body
        assert body["type"].endswith("/conflict"), body
        # Detail should mention the blocking channel id so operators
        # can find the cause without running follow-up queries.
        detail = body.get("detail", "")
        assert ch_id in detail, (
            f"expected blocking channel id {ch_id!r} in detail; got: {detail!r}"
        )
        body_str = json.dumps(body)
        assert "/errors/internal" not in body_str, body

        # Provider still exists (the failed DELETE must not partially
        # destroy state).
        r = await client.get(f"/v1/channel_providers/{cp_id}")
        assert r.status_code == 200, r.text

        # 4. DELETE channel → ok.
        r = await client.delete(f"/v1/channels/{ch_id}")
        assert r.status_code in (200, 204), r.text

        # 5. DELETE provider now succeeds.
        r = await client.delete(f"/v1/channel_providers/{cp_id}")
        assert r.status_code in (200, 204), r.text

        # Both are gone.
        r = await client.get(f"/v1/channel_providers/{cp_id}")
        assert r.status_code == 404, r.text
    finally:
        for url in cleanup_urls:
            try:
                await client.delete(url)
            except Exception:  # noqa: BLE001
                pass
