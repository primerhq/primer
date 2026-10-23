"""Draft ``_test`` of embedding and cross-encoder providers.

``POST /v1/embedding_providers/_test`` and ``POST /v1/cross_encoder_providers/_test`` (admin only, nothing stored) answer whether a provider the console has not saved yet
would work, the way the speech and web ``_test`` routes do: always HTTP 200, ``{"ok": true, ...}`` or ``{"ok": false, "error": ...}``. Before them a Test button on these
classes answered 405 (the literal path fell through to the CRUD ``/{id}`` route, which has no POST).

* ``openai`` and ``gemini`` list the models the server offers (the probe behind ``_discover_models``);
* ``huggingface`` (embedding and cross encoder) checks each named model on the Hub (``primer/common/hf_hub.py``).

Every upstream is mocked with ``respx`` at the HTTP boundary: nothing here reaches OpenAI, Google or the real Hugging Face Hub.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from primer.llm.gemini import GEMINI_BASE_URL

HUB = "https://hub.test"
EMB_URL = "http://emb.test/v1"
EMBEDDING = "/v1/embedding_providers/_test"
CROSS_ENCODER = "/v1/cross_encoder_providers/_test"
BGE = "BAAI/bge-small-en-v1.5"
RERANKER = "BAAI/bge-reranker-v2-m3"
KEY = "sk-draft-SECRET-5555"
TOKEN = "hf_secret_1234"


def _hub_url(name: str) -> str:
    return f"{HUB}/api/models/{name}/auth-check"


@pytest.fixture(autouse=True)
def _hub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_ENDPOINT", HUB)


def _openai_draft(url: str = EMB_URL, **config) -> dict:
    return {"provider": "openai", "config": {"url": url, **config}}


# ---- the routes exist and answer a draft that is not finished ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embedding_test_route_is_not_405(client) -> None:
    r = await client.post(EMBEDDING, json={})

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False and "invalid draft" in r.json()["error"]


@pytest.mark.asyncio
async def test_cross_encoder_test_route_is_not_405(client) -> None:
    r = await client.post(CROSS_ENCODER, json={})

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False and "invalid draft" in r.json()["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [EMBEDDING, CROSS_ENCODER])
async def test_a_draft_that_names_nothing_never_reaches_the_network(client, route: str) -> None:
    with respx.mock:
        r = await client.post(route, json={})

        assert r.json()["ok"] is False
        assert respx.calls.call_count == 0


@pytest.mark.asyncio
async def test_the_literal_test_path_does_not_change_how_an_id_is_read(client) -> None:
    """Mounted before the CRUD router: GET on the same path is still the CRUD read of a row called "_test" (none)."""
    assert (await client.get("/v1/embedding_providers/_test")).status_code == 404
    assert (await client.get("/v1/cross_encoder_providers/_test")).status_code == 404


# ---- openai ------------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_openai_draft_lists_models(client) -> None:
    route = respx.get(f"{EMB_URL}/models").mock(return_value=httpx.Response(200, json={"data": [{"id": "text-embedding-3-small"}]}))

    r = await client.post(EMBEDDING, json=_openai_draft(api_key=KEY))

    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "models": ["text-embedding-3-small"]}
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {KEY}", "the probe authenticates with the key that was typed"
    assert KEY not in r.text


@pytest.mark.asyncio
@respx.mock
async def test_openai_draft_needs_only_the_connection_fields(client) -> None:
    """No id, no models, no limits, no key: a keyless local server is a valid draft."""
    route = respx.get(f"{EMB_URL}/models").mock(return_value=httpx.Response(200, json={"data": [{"id": "nomic-embed-text"}, {"id": "qwen3-chat"}]}))

    r = await client.post(EMBEDDING, json=_openai_draft())

    assert r.json() == {"ok": True, "models": ["nomic-embed-text", "qwen3-chat"]}
    assert "authorization" not in route.calls.last.request.headers


@pytest.mark.asyncio
@respx.mock
async def test_id_and_limits_of_a_draft_are_ignored(client) -> None:
    respx.get(f"{EMB_URL}/models").mock(return_value=httpx.Response(200, json={"data": [{"id": "m"}]}))
    draft = {**_openai_draft(), "id": "not a valid id!", "limits": "garbage", "models": [{"name": "m"}]}

    r = await client.post(EMBEDDING, json=draft)

    assert r.json() == {"ok": True, "models": ["m"]}


@pytest.mark.asyncio
@respx.mock
async def test_an_upstream_failure_never_echoes_the_key(client) -> None:
    respx.get(f"{EMB_URL}/models").mock(side_effect=httpx.ConnectError(f"refused, with Authorization: Bearer {KEY} in the request"))

    r = await client.post(EMBEDDING, json=_openai_draft(api_key=KEY))

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
    assert KEY not in r.text
    assert "[REDACTED]" in r.json()["error"], "the key should be masked, not dropped silently"


@pytest.mark.asyncio
@respx.mock
async def test_an_upstream_failure_masks_the_password_of_a_base_url(client) -> None:
    url = "http://svc:s3cr3t-pw@emb.test/v1"
    respx.route(host="emb.test").mock(side_effect=httpx.ConnectError(f"All connection attempts failed for url '{url}/models'"))

    r = await client.post(EMBEDDING, json=_openai_draft(url))

    assert r.json()["ok"] is False
    assert "s3cr3t-pw" not in r.text
    assert "[REDACTED]" in r.json()["error"]


@pytest.mark.asyncio
@respx.mock
async def test_a_refusing_server_is_a_not_ok_answer_with_its_status(client) -> None:
    respx.get(f"{EMB_URL}/models").mock(return_value=httpx.Response(401, json={"error": "bad key"}))

    r = await client.post(EMBEDDING, json=_openai_draft(api_key=KEY))

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False and "401" in r.json()["error"]
    assert KEY not in r.text


# ---- gemini ------------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_gemini_draft_lists_embed_content_models(client) -> None:
    route = respx.get(f"{GEMINI_BASE_URL}/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"], "inputTokenLimit": 2048},
                ],
            },
        ),
    )

    r = await client.post(EMBEDDING, json={"provider": "gemini", "config": {"api_key": "AIza-test-KEY-9876"}})

    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "models": ["gemini-embedding-001"]}
    assert route.calls.last.request.headers["x-goog-api-key"] == "AIza-test-KEY-9876"


@pytest.mark.asyncio
@respx.mock
async def test_googles_400_for_a_bad_gemini_key_is_the_key_message(client) -> None:
    """What Google answers for a bad key: HTTP 400, INVALID_ARGUMENT, reason API_KEY_INVALID (audit G2-19); the 401 below
    is the other shape the probe still maps."""
    body = {"error": {
        "code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT",
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID", "domain": "googleapis.com",
                     "metadata": {"service": "generativelanguage.googleapis.com"}}],
    }}
    respx.get(f"{GEMINI_BASE_URL}/models").mock(return_value=httpx.Response(400, json=body))

    r = await client.post(EMBEDDING, json={"provider": "gemini", "config": {"api_key": "AIza-test-KEY-9876"}})

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False and "Gemini API key invalid or unauthorized (HTTP 400)" in r.json()["error"]
    assert "AIza-test-KEY-9876" not in r.text


@pytest.mark.asyncio
@respx.mock
async def test_a_refused_gemini_key_is_a_not_ok_answer_without_the_key(client) -> None:
    respx.get(f"{GEMINI_BASE_URL}/models").mock(return_value=httpx.Response(401, json={"error": {"message": "API key not valid. AIza-test-KEY-9876"}}))

    r = await client.post(EMBEDDING, json={"provider": "gemini", "config": {"api_key": "AIza-test-KEY-9876"}})

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False and "Gemini API key invalid or unauthorized (HTTP 401)" in r.json()["error"]
    assert "AIza-test-KEY-9876" not in r.text


# ---- huggingface (embedding) --------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_huggingface_draft_checks_each_model(client) -> None:
    respx.get(_hub_url(BGE)).mock(return_value=httpx.Response(200))
    respx.get(_hub_url("nope/missing")).mock(return_value=httpx.Response(404, headers={"X-Error-Code": "RepoNotFound"}))
    draft = {"provider": "huggingface", "config": {"token": TOKEN}, "models": [{"name": BGE}, {"name": "nope/missing"}]}

    r = await client.post(EMBEDDING, json=draft)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["models"] == [BGE], "the models that passed"
    assert [(c["name"], c["status"]) for c in body["checks"]] == [(BGE, "ok"), ("nope/missing", "not_found")]
    assert "1 of 2 models failed the Hub check: nope/missing (not_found)" in body["error"]
    assert {call.request.headers["authorization"] for call in respx.calls} == {f"Bearer {TOKEN}"}
    assert TOKEN not in r.text


@pytest.mark.asyncio
@respx.mock
async def test_huggingface_draft_that_passes_has_no_error(client) -> None:
    respx.get(_hub_url(BGE)).mock(return_value=httpx.Response(200))
    respx.get(_hub_url("Qwen/Qwen3-Embedding-0.6B")).mock(return_value=httpx.Response(200))

    r = await client.post(EMBEDDING, json={"provider": "huggingface", "models": [{"name": BGE}, {"name": "Qwen/Qwen3-Embedding-0.6B"}]})

    body = r.json()
    assert body["ok"] is True and "error" not in body
    assert body["models"] == [BGE, "Qwen/Qwen3-Embedding-0.6B"]
    assert all("authorization" not in call.request.headers for call in respx.calls), "no token typed, none sent (the config may be left out)"


@pytest.mark.asyncio
@respx.mock
async def test_huggingface_draft_names_every_model_that_failed(client) -> None:
    respx.get(_hub_url("a/one")).mock(return_value=httpx.Response(403, headers={"X-Error-Code": "GatedRepo"}))
    respx.get(_hub_url("a/two")).mock(return_value=httpx.Response(500))

    r = await client.post(EMBEDDING, json={"provider": "huggingface", "config": {}, "models": [{"name": "a/one"}, {"name": "a/two"}]})

    body = r.json()
    assert body["ok"] is False and body["models"] == []
    assert body["error"] == "2 of 2 models failed the Hub check: a/one (gated), a/two (error)"


@pytest.mark.asyncio
@pytest.mark.parametrize("models", [None, []])
async def test_huggingface_draft_without_models_is_invalid(client, models) -> None:
    draft = {"provider": "huggingface", "config": {}}
    if models is not None:
        draft["models"] = models

    with respx.mock:
        r = await client.post(EMBEDDING, json=draft)

        assert r.json() == {"ok": False, "error": "invalid draft: models: give at least one model name to check"}
        assert respx.calls.call_count == 0


@pytest.mark.asyncio
async def test_a_huggingface_draft_of_more_than_fifty_models_is_invalid(client) -> None:
    models = [{"name": f"org/model-{i}"} for i in range(51)]

    with respx.mock:
        r = await client.post(EMBEDDING, json={"provider": "huggingface", "models": models})

        assert r.json() == {"ok": False, "error": "invalid draft: models: at most 50 model names can be checked at once"}
        assert respx.calls.call_count == 0


# ---- huggingface (cross encoder) ----------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_cross_encoder_draft_checks_models_with_max_pair_length(client) -> None:
    route = respx.get(_hub_url(RERANKER)).mock(return_value=httpx.Response(200))
    draft = {"provider": "huggingface", "config": {"token": TOKEN}, "models": [{"name": RERANKER, "max_pair_length": 512}]}

    r = await client.post(CROSS_ENCODER, json=draft)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["models"] == [RERANKER] and len(body["checks"]) == 1
    assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
@respx.mock
async def test_cross_encoder_provider_may_be_left_out_because_there_is_only_one(client) -> None:
    respx.get(_hub_url(RERANKER)).mock(return_value=httpx.Response(200))

    r = await client.post(CROSS_ENCODER, json={"models": [{"name": RERANKER}]})

    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_cross_encoder_draft_without_models_is_invalid(client) -> None:
    r = await client.post(CROSS_ENCODER, json={"provider": "huggingface", "config": {}})

    assert r.json() == {"ok": False, "error": "invalid draft: models: give at least one model name to check"}


# ---- a draft that does not validate: the class that would hold it says why ----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "draft", "fragment"),
    [
        (EMBEDDING, {"provider": "nonesuch", "config": {}}, "provider"),
        (EMBEDDING, {"config": {}}, "provider"),
        (EMBEDDING, {"provider": "openai", "config": {}}, "url"),
        (EMBEDDING, {"provider": "openai"}, "url"),
        (EMBEDDING, {"provider": "openai", "config": {"url": "not a url"}}, "url"),
        (EMBEDDING, {"provider": "openai", "config": "nope"}, "config"),
        (EMBEDDING, {"provider": "gemini", "config": {"api_key": ["x"]}}, "api_key"),
        (EMBEDDING, {"provider": "huggingface", "config": {"token": 5}, "models": [{"name": BGE}]}, "token"),
        (EMBEDDING, {"provider": "huggingface", "models": [{"name": ""}]}, "name"),
        (EMBEDDING, {"provider": "huggingface", "models": ["a bare string is not a model row"]}, "models"),
        (CROSS_ENCODER, {"provider": "nonesuch", "config": {}}, "provider"),
        (CROSS_ENCODER, {"provider": "huggingface", "models": [{"name": RERANKER, "max_pair_length": 0}]}, "max_pair_length"),
        (CROSS_ENCODER, {"provider": "huggingface", "config": {"token": ["x"]}, "models": [{"name": RERANKER}]}, "token"),
    ],
)
async def test_a_draft_the_model_class_refuses_is_an_invalid_draft_naming_the_field(client, route: str, draft: dict, fragment: str) -> None:
    with respx.mock:
        r = await client.post(route, json=draft)

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False and body["error"].startswith("invalid draft: "), body
        assert fragment in body["error"], body["error"]
        assert respx.calls.call_count == 0, "a draft that does not validate asks nobody"


@pytest.mark.asyncio
async def test_an_unknown_provider_lists_the_kinds_there_are(client) -> None:
    r = await client.post(EMBEDDING, json={"provider": "nonesuch", "config": {}})

    error = r.json()["error"]
    assert "openai" in error and "gemini" in error and "huggingface" in error, error


# Longer than 50 characters on purpose: pydantic cuts a long ``input_value`` to its first 25 and last 24 characters (see test_probe_routes_do_not_echo_credentials.py).
_UNVALIDATED_URLS = [
    "http://svc:s3cr3t-pw-0123456789abcdefghij@emb.test:notaport/v1",
    "http://u:s3cr3t/pw-0123456789abcdefghijklmnopqrstuv@emb.test:notaport/v1",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", _UNVALIDATED_URLS)
async def test_a_draft_that_does_not_validate_does_not_print_the_url_it_was_given(client, url: str) -> None:
    assert len(url) > 50

    r = await client.post(EMBEDDING, json=_openai_draft(url))

    error = r.json()["error"]
    assert r.json()["ok"] is False and error.startswith("invalid draft"), error
    assert "s3cr3t" not in error and "pw@" not in error and "0123456789" not in error, error
    assert "url" in error, "the person still needs to know WHICH field is wrong"


@pytest.mark.asyncio
async def test_a_draft_that_does_not_validate_does_not_print_the_token_it_was_given(client) -> None:
    long_token = "hf_" + "0123456789abcdef" * 4
    draft = {"provider": "huggingface", "config": {"token": long_token}, "models": [{"name": ""}]}

    r = await client.post(EMBEDDING, json=draft)

    assert r.json()["ok"] is False and long_token not in r.text and "0123456789abcdef" not in r.text


# ---- nothing is stored ----------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_a_test_stores_nothing(client) -> None:
    respx.get(f"{EMB_URL}/models").mock(return_value=httpx.Response(200, json={"data": [{"id": "m"}]}))
    respx.get(_hub_url(BGE)).mock(return_value=httpx.Response(200))
    respx.get(_hub_url(RERANKER)).mock(return_value=httpx.Response(200))
    before = {
        plural: (await client.get(f"/v1/{plural}")).json()["total"] for plural in ("embedding_providers", "cross_encoder_providers")
    }

    await client.post(EMBEDDING, json=_openai_draft())
    await client.post(EMBEDDING, json={"provider": "huggingface", "models": [{"name": BGE}]})
    await client.post(CROSS_ENCODER, json={"models": [{"name": RERANKER}]})

    after = {plural: (await client.get(f"/v1/{plural}")).json()["total"] for plural in ("embedding_providers", "cross_encoder_providers")}
    assert after == before


# ---- who may ask ----------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [EMBEDDING, CROSS_ENCODER])
@pytest.mark.parametrize("role", ["user", "restricted"])
async def test_a_role_user_is_refused(raw_client, app, route: str, role: str) -> None:
    """A test spends the deployment's network on a credential the caller supplies: system configuration, admin only (the helper routers are mounted with the admin gate)."""
    from tests.api.test_require_user_admin import _login, _seed

    await _seed(app, uid=f"u-{role}", username=role, role=role)
    await _login(raw_client, role)

    r = await raw_client.post(route, json=_openai_draft())

    assert r.status_code == 403, r.text
    assert r.json()["extensions"]["error"] == "forbidden_role"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [EMBEDDING, CROSS_ENCODER])
async def test_an_anonymous_caller_is_refused(raw_client, route: str) -> None:
    r = await raw_client.post(route, json={})

    assert r.status_code == 401, r.text
