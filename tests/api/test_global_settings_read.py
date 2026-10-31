"""ACL-P4 Task 9: global settings readable by every full user, writes admin-only.

The install-wide settings a user benefits from knowing are readable by any
full user, with every secret served masked: the web-search and web-fetch
providers plus their active configs, the speech (STT/TTS) providers plus
the active speech config, the artifact-storage providers plus the active
store, canvas settings, and the MCP exposure singleton (not its ``/available`` catalogue, which is exposure administration and
stays admin-only). Restricted users
still get nothing, unauthenticated callers get 401, and every write
(create / update / delete, the ``_test`` probes, ``/find``, and the
active-config / canvas / exposure PUTs) stays admin-only. Red before the
admin-tier split of the global-settings routers.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from pydantic import SecretStr

from tests.api.conftest import app, raw_client as client  # noqa: F401

from primer.auth.passwords import hash_password
from primer.model.common import dump_for_storage
from primer.model.provider import (
    Limits,
    SpeechToTextConfig,
    SpeechToTextProvider,
    TextToSpeechConfig,
    TextToSpeechProvider,
)
from primer.model.providers.artifact import (
    ArtifactStorageProvider,
    S3ArtifactConfig,
)
from primer.model.user import User
from primer.model.web_fetch import JinaFetchConfig, WebFetchProvider
from primer.model.web_search import TavilyConfig, WebSearchProvider

# The five seeded rows carry these secrets; no user-tier read may serve one.
SECRETS = {
    "ws-tav-1": "tav-secret-1",
    "wf-jina-1": "jina-secret-2",
    "stt-1": "stt-secret-3",
    "tts-1": "tts-secret-4",
    "as-s3-1": ("as-access-5", "as-secret-6"),
}
# URL userinfo a non-admin read must not carry at all (security review of P4, finding 6); admins keep today's view.
STT_URL = "http://svc:stt-url-pw-7@asr.local:8006/v1"
S3_ENDPOINT = "http://minio:s3-url-pw-8@minio.local:9000"

# Every global-settings read: user tier (masked secrets), restricted 403.
READ_ROUTES = [
    "GET /v1/web_search_providers",
    "GET /v1/web_search_providers/ws-tav-1",
    "GET /v1/web_search_providers/_types",
    "GET /v1/web_search_active_config",
    "GET /v1/web_fetch_providers",
    "GET /v1/web_fetch_providers/wf-jina-1",
    "GET /v1/web_fetch_providers/_types",
    "GET /v1/web_fetch_active_config",
    "GET /v1/stt_providers",
    "GET /v1/stt_providers/stt-1",
    "GET /v1/stt_providers/_types",
    "GET /v1/tts_providers",
    "GET /v1/tts_providers/tts-1",
    "GET /v1/tts_providers/_types",
    "GET /v1/speech_active_config",
    "GET /v1/artifact_storage_providers",
    "GET /v1/artifact_storage_providers/as-s3-1",
    "GET /v1/artifact_storage_providers/_types",
    "GET /v1/artifact_storage_active_config",
    "GET /v1/canvas_settings",
    "GET /v1/mcp_exposure",
]

# Exposure administration, not a global setting: the catalogue lists (and contacts) every toolset row, private ones
# included, so it stays admin-only (security review of P4, finding 4).
ADMIN_READS = ["GET /v1/mcp_exposure/available"]

# Every global-settings write stays admin: a user (and a restricted user)
# gets 403 forbidden_role on each, before the body is even read.
WRITE_CALLS = [
    ("POST", "/v1/web_search_providers", {"id": "ws-new", "provider_type": "exa",
                                          "config": {"type": "exa", "api_key": "x"}}),
    ("PUT", "/v1/web_search_providers/ws-tav-1", {}),
    ("DELETE", "/v1/web_search_providers/ws-tav-1", None),
    ("POST", "/v1/web_search_providers/_test", {"provider": "tavily",
                                                "config": {"type": "tavily", "api_key": "x"}}),
    ("POST", "/v1/web_search_providers/find", {"predicate": None,
                                               "page": {"offset": 0, "limit": 10}}),
    ("PUT", "/v1/web_search_active_config",
     {"config": {"mode": "single", "provider_id": "ws-tav-1"}}),
    ("PUT", "/v1/web_fetch_active_config",
     {"config": {"mode": "single", "provider_id": "wf-jina-1"}}),
    ("PUT", "/v1/speech_active_config", {"stt_provider_id": "stt-1"}),
    ("PUT", "/v1/artifact_storage_active_config", {"provider_id": "as-s3-1"}),
    ("PUT", "/v1/canvas_settings", {}),
    ("PUT", "/v1/mcp_exposure", {"enabled": True}),
]


async def _seed_user(app, *, uid: str, username: str, role: str) -> None:
    storage = app.state.storage_provider.get_storage(User)
    await storage.create(
        User(
            id=uid,
            username=username,
            password_hash=await hash_password("pw"),
            created_at=datetime.now(timezone.utc),
            role=role,
        )
    )


async def _login(client, username: str) -> None:
    client.cookies.clear()
    resp = await client.post(
        "/v1/auth/login",
        json={"username": username, "password": "pw"},
    )
    assert resp.status_code == 200, resp.text


def _mask_tail(value: str) -> str:
    """The ApiKeySecret wire mask: the placeholder plus the secret's tail."""
    return "**********" + value[-4:]


@pytest_asyncio.fixture
async def gs(client, app):
    await _seed_user(app, uid="gs-admin", username="gs-admin", role="admin")
    await _seed_user(app, uid="gs-user", username="gs-user", role="user")
    await _seed_user(app, uid="gs-restr", username="gs-restr", role="restricted")
    storage = app.state.storage_provider
    await storage.get_storage(WebSearchProvider).create(
        WebSearchProvider(
            id="ws-tav-1",
            provider_type="tavily",
            config=TavilyConfig(api_key=SecretStr(SECRETS["ws-tav-1"])),
        )
    )
    await storage.get_storage(WebFetchProvider).create(
        WebFetchProvider(
            id="wf-jina-1",
            provider_type="jina",
            config=JinaFetchConfig(api_key=SecretStr(SECRETS["wf-jina-1"])),
        )
    )
    await storage.get_storage(SpeechToTextProvider).create(
        SpeechToTextProvider(
            id="stt-1",
            provider="openai",
            default_model="whisper-1",
            config=SpeechToTextConfig(
                url=STT_URL,
                api_key=SecretStr(SECRETS["stt-1"]),
            ),
            limits=Limits(max_concurrency=1),
        )
    )
    await storage.get_storage(TextToSpeechProvider).create(
        TextToSpeechProvider(
            id="tts-1",
            provider="openai",
            default_model="kokoro",
            default_voice="af_heart",
            config=TextToSpeechConfig(
                url="http://tts.local:8004/v1",
                api_key=SecretStr(SECRETS["tts-1"]),
            ),
            limits=Limits(max_concurrency=1),
        )
    )
    await storage.get_storage(ArtifactStorageProvider).create(
        ArtifactStorageProvider(
            id="as-s3-1",
            provider="s3",
            config=S3ArtifactConfig(
                bucket="gs-bucket",
                endpoint_url=S3_ENDPOINT,
                access_key=SecretStr(SECRETS["as-s3-1"][0]),
                secret_key=SecretStr(SECRETS["as-s3-1"][1]),
            ),
        )
    )
    yield


def _raw_secrets() -> list[str]:
    flat = ["stt-url-pw-7", "s3-url-pw-8", "svc:", "minio:"]
    for value in SECRETS.values():
        flat.extend(value if isinstance(value, tuple) else (value,))
    return flat


@pytest.mark.asyncio
async def test_user_reads_every_global_setting_with_secrets_masked(gs, app, client) -> None:
    await _login(client, "gs-user")
    bodies = {}
    for route in READ_ROUTES:
        method, path = route.split(" ", 1)
        resp = await client.request(method, path)
        assert resp.status_code == 200, f"{route} as a user: {resp.status_code} {resp.text}"
        bodies[route] = resp.json()

    # No raw secret anywhere in any read answer.
    import json as _json

    for route, body in bodies.items():
        text = _json.dumps(body)
        for secret in _raw_secrets():
            assert secret not in text, f"{route} served the raw secret {secret!r}"

    ws = bodies["GET /v1/web_search_providers/ws-tav-1"]
    assert ws["config"]["api_key"] == "**********"
    wf = bodies["GET /v1/web_fetch_providers/wf-jina-1"]
    assert wf["config"]["api_key"] == "**********"
    # A non-admin sees no trailing characters of a key, and no userinfo on a URL (security review of P4, finding 6).
    stt = bodies["GET /v1/stt_providers/stt-1"]
    assert stt["config"]["api_key"] == "**********"
    assert stt["config"]["url"] == "http://asr.local:8006/v1"
    tts = bodies["GET /v1/tts_providers/tts-1"]
    assert tts["config"]["api_key"] == "**********"
    asp = bodies["GET /v1/artifact_storage_providers/as-s3-1"]
    assert asp["config"]["access_key"] == "**********"
    assert asp["config"]["secret_key"] == "**********"
    assert asp["config"]["endpoint_url"] == "http://minio.local:9000"
    listed = bodies["GET /v1/stt_providers"]
    assert listed["items"][0]["config"]["api_key"] == "**********"
    # The masked value is not the stored plaintext: a write-context unmask
    # never leaks through a user-tier read.
    stored = await app.state.storage_provider.get_storage(
        WebSearchProvider
    ).get("ws-tav-1")
    assert stored.config.api_key.get_secret_value() == SECRETS["ws-tav-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("write", WRITE_CALLS, ids=[f"{m} {p}" for m, p, _ in WRITE_CALLS])
async def test_user_writes_are_refused(gs, client, write) -> None:
    method, path, body = write
    await _login(client, "gs-user")
    resp = await client.request(method, path, json=body)
    assert resp.status_code == 403, f"{method} {path} as a user: {resp.status_code} {resp.text}"
    assert resp.json()["extensions"]["error"] == "forbidden_role"


@pytest.mark.asyncio
async def test_exposure_administration_reads_are_admin_only(gs, client) -> None:
    for username, expected in (("gs-user", 403), ("gs-restr", 403), ("gs-admin", 200)):
        await _login(client, username)
        for route in ADMIN_READS:
            method, path = route.split(" ", 1)
            resp = await client.request(method, path)
            assert resp.status_code == expected, f"{route} as {username}: {resp.status_code} {resp.text}"
            if expected == 403:
                assert resp.json()["extensions"]["error"] == "forbidden_role"


@pytest.mark.asyncio
async def test_restricted_reads_are_refused(gs, client) -> None:
    await _login(client, "gs-restr")
    for route in READ_ROUTES:
        method, path = route.split(" ", 1)
        resp = await client.request(method, path)
        assert resp.status_code == 403, f"{route} as restricted: {resp.status_code}"
        assert resp.json()["extensions"]["error"] == "forbidden_role"
    method, path, body = WRITE_CALLS[0]
    resp = await client.request(method, path, json=body)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_unauthenticated_is_refused(gs, client) -> None:
    client.cookies.clear()
    resp = await client.get("/v1/web_search_providers")
    assert resp.status_code == 401
    assert resp.json()["extensions"]["error"] == "auth_required"
    resp = await client.put("/v1/web_search_active_config", json={})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_reads_and_writes_are_unchanged(gs, client) -> None:
    await _login(client, "gs-admin")
    for route in READ_ROUTES:
        method, path = route.split(" ", 1)
        resp = await client.request(method, path)
        assert resp.status_code == 200, f"{route} as admin: {resp.status_code} {resp.text}"

    # An admin's view is unchanged: a key's tail, a URL password masked, the S3 endpoint as typed.
    stt = (await client.get("/v1/stt_providers/stt-1")).json()["config"]
    assert stt["api_key"] == _mask_tail(SECRETS["stt-1"]) and stt["url"] == "http://svc:**********@asr.local:8006/v1"
    asp = (await client.get("/v1/artifact_storage_providers/as-s3-1")).json()["config"]
    assert asp["endpoint_url"] == S3_ENDPOINT

    # Create / update / delete a provider row, as an admin.
    created = await client.post(
        "/v1/web_fetch_providers",
        json=dump_for_storage(
            WebFetchProvider(
                id="wf-exa-9",
                provider_type="exa",
                config={"type": "exa", "api_key": "wf-exa-secret-9"},
            )
        ),
    )
    assert created.status_code == 201, created.text
    got = await client.get("/v1/web_fetch_providers/wf-exa-9")
    assert got.status_code == 200
    assert got.json()["config"]["api_key"] == "**********"
    resp = await client.delete("/v1/web_fetch_providers/wf-exa-9")
    assert resp.status_code in (200, 204)

    # The four active-config writes.
    resp = await client.put(
        "/v1/web_search_active_config",
        json={"config": {"mode": "single", "provider_id": "ws-tav-1"}},
    )
    assert resp.status_code == 200, resp.text
    resp = await client.put(
        "/v1/web_fetch_active_config",
        json={"config": {"mode": "single", "provider_id": "wf-jina-1"}},
    )
    assert resp.status_code == 200, resp.text
    resp = await client.put(
        "/v1/speech_active_config",
        json={"stt_provider_id": "stt-1", "tts_provider_id": "tts-1", "tts_voice": None},
    )
    assert resp.status_code == 200, resp.text
    # The active store switch must name a store this process can use: the
    # one already active (the seeded DB default), which the check accepts.
    active = await client.get("/v1/artifact_storage_active_config")
    assert active.status_code == 200, active.text
    current = active.json()["provider_id"]
    resp = await client.put(
        "/v1/artifact_storage_active_config",
        json={"provider_id": current},
    )
    assert resp.status_code == 200, resp.text

    # Canvas settings and the MCP exposure singleton.
    resp = await client.put("/v1/canvas_settings", json={})
    assert resp.status_code == 200, resp.text
    resp = await client.put("/v1/mcp_exposure", json={"enabled": False})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_the_non_admin_view_ends_with_the_read(gs, app, client) -> None:
    """The non-admin mode lives for one read only: the next admin read and any later dump keep the admin view."""
    from primer.model.common import SERVING_NON_ADMIN

    await _login(client, "gs-user")
    assert (await client.get("/v1/stt_providers/stt-1")).json()["config"]["api_key"] == "**********"
    assert SERVING_NON_ADMIN.get() is False
    row = await app.state.storage_provider.get_storage(SpeechToTextProvider).get("stt-1")
    assert row.model_dump(mode="json")["config"]["api_key"] == _mask_tail(SECRETS["stt-1"])

    await _login(client, "gs-admin")
    assert (await client.get("/v1/stt_providers/stt-1")).json()["config"]["api_key"] == _mask_tail(SECRETS["stt-1"])
