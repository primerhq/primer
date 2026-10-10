"""Over REST on a real SQLite store: a masked secret is kept only for the origin it was stored for, and a served mask is refused on create (ticket 01a1212a).

``PUT`` is a full replace, ``GET`` serves the secret masked, and ``preserve_masked_secrets`` swapped the mask back for the stored value whatever the base URL became: an update of
``{url: "https://attacker.example/v1", api_key: "<the mask>"}`` stored the real key next to the attacker's host. It answers a 422 now (``re-enter the secret: the stored one is kept only for the same
host``) and the row is untouched; the same host with another path keeps the key; a key the person typed is theirs. ``POST`` of a served body under a new id (the copy-a-provider move) used to store
the masks as the values; it is a 422 too.
"""

from __future__ import annotations

import pytest

from primer.model.providers.embedding import EmbeddingProvider
from primer.model.providers.llm import LLMProvider
from primer.model.providers.speech import SpeechToTextProvider, TextToSpeechProvider
from primer.model.provider import SemanticSearchProvider
from tests.api.test_provider_url_userinfo_sqlite import client, sp  # noqa: F401  (fixtures: an app on a real SQLite store)

KEY = "sk-live-0123456789abcdef"
HOME = "https://home.example/v1"
AWAY = "https://attacker.example/v1"

FAMILIES = {
    "openchat": ("llm_providers", LLMProvider, {"id": "o-1", "provider": "openchat", "models": [{"name": "m", "context_length": 8192}], "config": {"url": HOME, "api_key": KEY, "flavor": "other"}, "limits": {"max_concurrency": 1}}),
    "ollama": ("llm_providers", LLMProvider, {"id": "o-2", "provider": "ollama", "models": [{"name": "m", "context_length": 8192}], "config": {"url": HOME, "api_key": KEY}, "limits": {"max_concurrency": 1}}),
    "embedding": ("embedding_providers", EmbeddingProvider, {"id": "o-3", "provider": "openai", "models": [{"name": "m"}], "config": {"url": HOME, "api_key": KEY}, "limits": {"max_concurrency": 1}}),
    "speech-to-text": ("stt_providers", SpeechToTextProvider, {"id": "o-4", "provider": "openai", "default_model": "whisper-1", "config": {"url": HOME, "api_key": KEY}, "limits": {"max_concurrency": 1}}),
    "text-to-speech": ("tts_providers", TextToSpeechProvider, {"id": "o-5", "provider": "openai", "default_model": "tts-1", "default_voice": "alloy", "config": {"url": HOME, "api_key": KEY}, "limits": {"max_concurrency": 1}}),
}
IDS = sorted(FAMILIES)


async def _create(client, family: str) -> tuple[str, type, str, dict]:
    path, model, body = FAMILIES[family]
    created = await client.post(f"/v1/{path}", json=body)
    assert created.status_code in (200, 201), created.text
    return path, model, body["id"], (await client.get(f"/v1/{path}/{body['id']}")).json()


@pytest.mark.asyncio
@pytest.mark.parametrize("family", IDS)
async def test_the_key_is_served_masked_and_a_put_of_the_served_body_keeps_it(client, sp, family: str) -> None:
    path, model, row_id, served = await _create(client, family)
    assert served["config"]["api_key"].startswith("**********") and KEY not in str(served)
    served["limits"]["max_concurrency"] = 3

    r = await client.put(f"/v1/{path}/{row_id}", json=served)

    assert r.status_code == 200, r.text
    stored = await sp.get_storage(model).get(row_id)
    assert stored.config.api_key.get_secret_value() == KEY and stored.limits.max_concurrency == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("family", IDS)
async def test_a_put_that_moves_the_host_and_keeps_the_key_mask_is_a_422_and_stores_nothing(client, sp, family: str) -> None:
    path, model, row_id, served = await _create(client, family)
    served["config"]["url"] = AWAY

    r = await client.put(f"/v1/{path}/{row_id}", json=served)

    assert r.status_code == 422, r.text
    assert "re-enter the secret" in r.text and KEY not in r.text
    stored = await sp.get_storage(model).get(row_id)
    assert str(stored.config.url) == HOME and stored.config.api_key.get_secret_value() == KEY, "the row is untouched"


_BYPASS = [
    pytest.param("https:\t//attacker.example\\@home.example/mcp/x://y", id="a tab in the scheme separator"),
    pytest.param("https:\n//attacker.example\\@home.example/mcp/x://y", id="a newline in the scheme separator"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("bypass", _BYPASS)
async def test_an_mcp_url_that_a_scan_for_the_first_scheme_separator_misreads_is_a_move_and_a_422(client, sp, bypass: str) -> None:
    """``HttpConfig.url`` is a plain string, so nothing normalises ``https:<TAB>//attacker.example\\@home.example/...``: ``urlsplit`` drops the tab and reads ``home.example`` (the stored host) while a
    WHATWG client reads ``attacker.example``. The origin check called it the same origin and the REAL header secret was kept next to the attacker's host (round 2 review, N4b: ``PUT /v1/toolsets``
    answered 200)."""
    from primer.model.provider import Toolset

    body = {"id": "ts-a", "provider": "mcp", "config": {"transport": "http", "config": {"url": f"{HOME}/mcp", "headers": {"Authorization": f"Bearer {KEY}"}}}}
    created = await client.post("/v1/toolsets?allow_unreachable=true", json=body)
    assert created.status_code in (200, 201), created.text
    served = (await client.get("/v1/toolsets/ts-a")).json()
    served["config"]["config"]["url"] = bypass

    r = await client.put("/v1/toolsets/ts-a?allow_unreachable=true", json=served)

    assert r.status_code == 422, r.text
    assert "re-enter the" in r.text and KEY not in r.text
    stored = await sp.get_storage(Toolset).get("ts-a")
    assert stored.config.config.url == f"{HOME}/mcp" and stored.config.config.headers["Authorization"].get_secret_value() == f"Bearer {KEY}", "the row is untouched"


@pytest.mark.asyncio
@pytest.mark.parametrize("bypass", _BYPASS)
async def test_a_kubernetes_apiserver_url_that_is_misread_the_same_way_is_a_move_and_a_422(client, sp, bypass: str) -> None:
    from primer.model.workspace import WorkspaceProvider

    body = {
        "id": "wp-k", "provider": "kubernetes",
        "config": {
            "kind": "kubernetes", "variant": "system", "namespace": "ns", "reachability": {"kind": "in_cluster"},
            "connection": {"kind": "service_account_token", "apiserver_url": f"{HOME}:6443", "ca_data": "ca", "token": KEY},
        },
    }
    created = await client.post("/v1/workspace_providers", json=body)
    assert created.status_code in (200, 201), created.text
    served = (await client.get("/v1/workspace_providers/wp-k")).json()
    served["config"]["connection"]["apiserver_url"] = bypass.replace("/mcp/x://y", ":6443/x://y")

    r = await client.put("/v1/workspace_providers/wp-k", json=served)

    assert r.status_code == 422, r.text
    assert "re-enter the" in r.text and KEY not in r.text
    stored = await sp.get_storage(WorkspaceProvider).get("wp-k")
    assert stored.config.connection.apiserver_url == f"{HOME}:6443" and stored.config.connection.token.get_secret_value() == KEY


@pytest.mark.asyncio
@pytest.mark.parametrize("family", IDS)
async def test_a_put_that_changes_only_the_path_keeps_the_key(client, sp, family: str) -> None:
    path, model, row_id, served = await _create(client, family)
    served["config"]["url"] = "https://home.example/v2"

    r = await client.put(f"/v1/{path}/{row_id}", json=served)

    assert r.status_code == 200, r.text
    stored = await sp.get_storage(model).get(row_id)
    assert str(stored.config.url) == "https://home.example/v2" and stored.config.api_key.get_secret_value() == KEY


@pytest.mark.asyncio
@pytest.mark.parametrize("family", IDS)
async def test_a_put_that_moves_the_host_with_a_new_key_stores_the_new_key(client, sp, family: str) -> None:
    path, model, row_id, served = await _create(client, family)
    served["config"]["url"] = AWAY
    served["config"]["api_key"] = "sk-live-a-different-key"

    r = await client.put(f"/v1/{path}/{row_id}", json=served)

    assert r.status_code == 200, r.text
    stored = await sp.get_storage(model).get(row_id)
    assert str(stored.config.url) == AWAY and stored.config.api_key.get_secret_value() == "sk-live-a-different-key"


@pytest.mark.asyncio
async def test_a_vector_store_password_is_bound_to_its_host(client, sp) -> None:
    body = {"id": "ssp-o", "provider": "pgvector", "config": {"hostname": "db.home.example", "port": 5432, "username": "u", "password": "s3cr3t-db-pw-0123", "database": "d"}}
    assert (await client.post("/v1/ssp", json=body)).status_code in (200, 201)
    served = (await client.get("/v1/ssp/ssp-o")).json()
    served["config"]["hostname"] = "db.attacker.example"

    r = await client.put("/v1/ssp/ssp-o", json=served)

    assert r.status_code == 422 and "re-enter the secret" in r.text and "s3cr3t-db-pw" not in r.text, r.text
    assert (await sp.get_storage(SemanticSearchProvider).get("ssp-o")).config.password.get_secret_value() == "s3cr3t-db-pw-0123"


# ---- create ---------------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("family", IDS)
async def test_a_post_of_a_served_body_under_a_new_id_is_a_422_and_stores_nothing(client, sp, family: str) -> None:
    """The copy-a-provider move: the served body carries the key's mask (and, for a credentialed URL, the URL password's), which would be stored as the values."""
    path, model, row_id, served = await _create(client, family)
    served["id"] = f"{row_id}-copy"

    r = await client.post(f"/v1/{path}", json=served)

    assert r.status_code == 422, r.text
    assert await sp.get_storage(model).get(f"{row_id}-copy") is None


@pytest.mark.asyncio
async def test_a_post_of_a_url_that_carries_the_served_password_mask_is_a_422(client, sp) -> None:
    body = {"id": "m-1", "provider": "openchat", "models": [{"name": "m", "context_length": 8192}], "config": {"url": "http://svc:**********@px.lan/v1", "flavor": "other"}, "limits": {"max_concurrency": 1}}

    r = await client.post("/v1/llm_providers", json=body)

    assert r.status_code == 422 and "re-enter the password" in r.text, r.text
    assert await sp.get_storage(LLMProvider).get("m-1") is None


# ---- a provider type that changed: there is no stored config of the new shape to restore the key from ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_provider_type_switch_that_sends_the_served_key_is_a_422_and_the_row_is_untouched(client, sp) -> None:
    """openchat -> openresponses with the served ``api_key`` and another host: the config class differs from the stored one, so the key was not restored but the mask was STORED as the key
    next to the new host (review of #711, nit N1). It is refused now."""
    path, model, row_id, served = await _create(client, "openchat")
    served["provider"] = "openresponses"
    served["config"]["url"] = AWAY

    r = await client.put(f"/v1/{path}/{row_id}", json=served)

    assert r.status_code == 422, r.text
    assert "re-enter the secret" in r.text and KEY not in r.text
    stored = await sp.get_storage(model).get(row_id)
    assert stored.provider.value == "openchat" and stored.config.api_key.get_secret_value() == KEY


@pytest.mark.asyncio
async def test_a_provider_type_switch_with_a_typed_key_is_stored(client, sp) -> None:
    path, model, row_id, served = await _create(client, "openchat")
    served["provider"] = "openresponses"
    served["config"]["url"] = AWAY
    served["config"]["api_key"] = "sk-live-a-different-key"

    r = await client.put(f"/v1/{path}/{row_id}", json=served)

    assert r.status_code == 200, r.text
    stored = await sp.get_storage(model).get(row_id)
    assert stored.provider.value == "openresponses" and stored.config.api_key.get_secret_value() == "sk-live-a-different-key"


@pytest.mark.asyncio
async def test_a_post_of_a_real_body_still_works(client, sp) -> None:
    body = {"id": "r-1", "provider": "openchat", "models": [{"name": "m", "context_length": 8192}], "config": {"url": "http://svc:s3cr3t@px.lan/v1", "api_key": KEY, "flavor": "other"}, "limits": {"max_concurrency": 1}}

    r = await client.post("/v1/llm_providers", json=body)

    assert r.status_code in (200, 201), r.text
