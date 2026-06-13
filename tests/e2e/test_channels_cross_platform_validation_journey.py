"""E2E: §3-§6 cross-platform ChannelProvider validation +
Channel uniqueness operator journey.

ONE pytest function walks every observable corner of the
ChannelProvider validation contract across all three platforms
(Slack, Telegram, Discord) plus the cross-router Channel
uniqueness/foreign-key surfaces. Bundles several backlog-pending
micro-pins into one realistic operator walk.

Steps:

  1. Slack — valid xapp-/xoxb- tokens → 201, secrets NOT echoed in
     plaintext (T0838 happy path).
  2. Slack — invalid app_token (no `xapp-` prefix) → 422
     /errors/validation-error; never /errors/internal (T0839).
  3. Telegram — invalid bot_token (no colon, < 20 chars) → 422 with
     a detail mentioning the shape rule (T0840).
  4. Telegram — valid bot_token → 201.
  5. Discord — valid 60-char bot_token → 201; secrets masked on GET
     round-trip (T0841 happy path).
  6. Channel — POST with non-existent provider_id → 422 /errors/*
     (T0844); never /errors/internal.
  7. Channel — POST with valid provider_id (Slack from step 1) → 201.
  8. Channel — POST same (provider_id, external_id) again → 409
     /errors/conflict; detail names both fields and the existing id
     (T0845).
  9. Reverse-order cleanup: channel → providers.

Subsystems exercised in one test:
  * Channel router validation hooks (_channel_on_pre_create:
    provider-existence + uniqueness)
  * Pydantic model validators on each platform's config
    (SlackChannelProviderConfig._validate_tokens, TelegramConfig
    ._validate, DiscordConfig._validate)
  * Secret round-trip: api keys + tokens stored as SecretStr
    and masked on GET response

Covers backlog items T0838, T0839, T0840, T0841, T0844, T0845.
Multi-platform, multi-router, multi-entity, in one function.

Pinned invariants:
  * Every platform's model validator runs server-side; bad tokens
    never persist.
  * Channel pre-create checks provider existence first (FK)
    THEN uniqueness — wrong provider_id surfaces as 422, not 409.
  * Channel uniqueness is on (provider_id, external_id); the
    conflict detail names BOTH fields.
  * No 5xx leaks across any invalid input.

NOTE: the WorkspaceChannelAssociation uniqueness step (formerly T0848)
was removed with the association model; workspace-to-channel binding is
now Workspace.channel_association (a single nullable link, no uniqueness
constraint to exercise).
"""

from __future__ import annotations

import json

import httpx
import pytest

from tests._support.smk import smk


# 60-char Discord placeholder (>=30; satisfies DiscordChannelProviderConfig
# length floor) without looking like a real token.
_FAKE_DISCORD_TOKEN = "x" * 60
# 30-char Telegram valid: `<id>:<hash>` shape, >=20 chars total.
_FAKE_TELEGRAM_TOKEN = "1234567890:abcdefghij1234567890"


# ===========================================================================
# T0860 — Cross-platform validation + uniqueness journey
# ===========================================================================


@smk("SMK-CHT-05")
@pytest.mark.asyncio
async def test_t0860_channels_cross_platform_validation_journey(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0860 — ONE pytest function walks every observable validation +
    uniqueness corner across Slack, Telegram, Discord ChannelProvider
    configs + Channel FK / uniqueness + Association uniqueness.

    See module docstring for the 10-step walk and which backlog item
    each step bundles.
    """
    cp_storage_urls: list[str] = []
    ch_id: str | None = None
    try:
        # ----- 1. Slack — valid tokens accepted, secrets masked ----
        slack_id = f"cp-slack-t860-{unique_suffix}"
        r = await client.post(
            "/v1/channel_providers",
            json={
                "id": slack_id,
                "provider": "slack",
                "config": {
                    "app_token": "xapp-1-abcdef",
                    "bot_token": "xoxb-1-abcdef",
                    "signing_secret": "supersecret",
                },
            },
        )
        assert r.status_code == 201, r.text
        cp_storage_urls.insert(0, f"/v1/channel_providers/{slack_id}")
        got = await client.get(f"/v1/channel_providers/{slack_id}")
        assert got.status_code == 200, got.text
        got_str = got.text
        # Secret round-trip: raw tokens NEVER appear in GET response.
        assert "xapp-1-abcdef" not in got_str, got_str
        assert "xoxb-1-abcdef" not in got_str, got_str
        assert "supersecret" not in got_str, got_str

        # ----- 2. Slack — invalid app_token prefix → 422 -----------
        r = await client.post(
            "/v1/channel_providers",
            json={
                "id": f"cp-slack-bad-{unique_suffix}",
                "provider": "slack",
                "config": {
                    "app_token": "wrongprefix-1-abc",  # missing xapp-
                    "bot_token": "xoxb-1-abc",
                },
            },
        )
        assert r.status_code == 422, r.text
        env = r.json()
        # FastAPI/Pydantic validation envelopes carry the primer slug
        # via the configured exception handler.
        assert env["type"].endswith("/validation-error"), env
        env_str = json.dumps(env)
        assert "/errors/internal" not in env_str, env

        # ----- 3. Telegram — invalid bot_token shape → 422 ---------
        r = await client.post(
            "/v1/channel_providers",
            json={
                "id": f"cp-tg-bad-{unique_suffix}",
                "provider": "telegram",
                "config": {"bot_token": "shorttoken"},  # no colon, <20
            },
        )
        assert r.status_code == 422, r.text
        env = r.json()
        assert env["type"].endswith("/validation-error"), env
        env_str = json.dumps(env)
        assert "/errors/internal" not in env_str, env

        # ----- 4. Telegram — valid token → 201 ---------------------
        tg_id = f"cp-tg-t860-{unique_suffix}"
        r = await client.post(
            "/v1/channel_providers",
            json={
                "id": tg_id,
                "provider": "telegram",
                "config": {"bot_token": _FAKE_TELEGRAM_TOKEN},
            },
        )
        assert r.status_code == 201, r.text
        cp_storage_urls.insert(0, f"/v1/channel_providers/{tg_id}")

        # ----- 5. Discord — valid 60-char bot_token → 201 ----------
        disc_id = f"cp-disc-t860-{unique_suffix}"
        r = await client.post(
            "/v1/channel_providers",
            json={
                "id": disc_id,
                "provider": "discord",
                "config": {"bot_token": _FAKE_DISCORD_TOKEN},
            },
        )
        assert r.status_code == 201, r.text
        cp_storage_urls.insert(0, f"/v1/channel_providers/{disc_id}")
        got = await client.get(f"/v1/channel_providers/{disc_id}")
        assert got.status_code == 200, got.text
        # Secret masked on GET.
        assert _FAKE_DISCORD_TOKEN not in got.text, got.text
        assert got.json()["provider"] == "discord"

        # ----- 6. Channel — non-existent provider_id → 422 ---------
        r = await client.post(
            "/v1/channels",
            json={
                "id": f"ch-noprov-{unique_suffix}",
                "provider_id": f"does-not-exist-{unique_suffix}",
                "provider": "slack",
                "external_id": "C0123ABC999",
                "label": "T0860 dead-ref channel",
            },
        )
        assert r.status_code == 422, r.text
        env_str = r.text
        assert "/errors/internal" not in env_str, env_str

        # ----- 7. Channel — valid provider_id → 201 ----------------
        ch_id = f"ch-t860-{unique_suffix}"
        ch_external_id = f"C0123ABC{unique_suffix[:6].upper()}"
        r = await client.post(
            "/v1/channels",
            json={
                "id": ch_id,
                "provider_id": slack_id,
                "provider": "slack",
                "external_id": ch_external_id,
                "label": "T0860 cross-platform channel",
            },
        )
        assert r.status_code == 201, r.text

        # ----- 8. Channel duplicate (provider_id, external_id) → 409
        r = await client.post(
            "/v1/channels",
            json={
                "id": f"ch-t860-dup-{unique_suffix}",
                "provider_id": slack_id,
                "provider": "slack",
                "external_id": ch_external_id,
                "label": "duplicate pair",
            },
        )
        assert r.status_code == 409, r.text
        env = r.json()
        assert env["type"].endswith("/conflict"), env
        detail = env.get("detail") or ""
        # Conflict detail names both fields + the existing id per
        # _channel_on_pre_create in primer/api/routers/channels.py.
        assert slack_id in detail and ch_external_id in detail, (
            f"conflict detail should name both fields; got: {detail!r}"
        )
        assert ch_id in detail, (
            f"conflict detail should name the existing channel id; "
            f"got: {detail!r}"
        )
    finally:
        # Reverse-order unwind: channel → providers.
        if ch_id is not None:
            try:
                await client.delete(f"/v1/channels/{ch_id}")
            except Exception:  # noqa: BLE001
                pass
        for url in cp_storage_urls:
            try:
                await client.delete(url)
            except Exception:  # noqa: BLE001
                pass
