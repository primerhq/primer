"""``POST /v1/huggingface/_check_models``: the Hub check as a route (admin only, nothing stored).

The console's Register provider overlay and an agent both ask it, before a Hugging Face embedding or cross-encoder provider is created, whether each model exists and
whether the token can pull it. The route drives the real app; the Hub is mocked with ``respx`` at the HTTP boundary (``HF_ENDPOINT``), so nothing here reaches the real
Hugging Face Hub. The mapping of each Hub answer is pinned in ``tests/common/test_hf_hub.py``.
"""

from __future__ import annotations

import httpx
import pytest
import respx

HUB = "https://hub.test"
GOOD = "BAAI/bge-small-en-v1.5"
MISSING = "nope/missing"
TOKEN = "hf_secret_1234"
ROUTE = "/v1/huggingface/_check_models"


def _url(name: str) -> str:
    return f"{HUB}/api/models/{name}/auth-check"


@pytest.fixture(autouse=True)
def _hub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_ENDPOINT", HUB)


@pytest.mark.asyncio
@respx.mock
async def test_the_route_answers_one_result_per_model(client) -> None:
    respx.get(_url(GOOD)).mock(return_value=httpx.Response(200))
    respx.get(_url(MISSING)).mock(return_value=httpx.Response(404, headers={"X-Error-Code": "RepoNotFound"}))

    r = await client.post(ROUTE, json={"models": [GOOD, MISSING], "token": TOKEN})

    assert r.status_code == 200, r.text
    assert r.json() == {
        "models": [
            {"name": GOOD, "ok": True, "status": "ok", "detail": "Can be pulled"},
            {"name": MISSING, "ok": False, "status": "not_found", "detail": "Not found, or private and the token cannot see it"},
        ],
    }
    assert [call.request.headers.get("authorization") for call in respx.calls] == [f"Bearer {TOKEN}"] * 2


@pytest.mark.asyncio
@respx.mock
async def test_the_token_is_optional(client) -> None:
    route = respx.get(_url(GOOD)).mock(return_value=httpx.Response(200))

    for body in ({"models": [GOOD]}, {"models": [GOOD], "token": None}):
        r = await client.post(ROUTE, json=body)

        assert r.status_code == 200, r.text
        assert r.json()["models"][0]["ok"] is True
    assert all("authorization" not in call.request.headers for call in route.calls)


@pytest.mark.asyncio
@respx.mock
async def test_an_unreachable_hub_is_a_200_with_an_error_result(client) -> None:
    respx.get(_url(GOOD)).mock(side_effect=httpx.ConnectError("refused"))

    r = await client.post(ROUTE, json={"models": [GOOD]})

    assert r.status_code == 200, r.text
    assert r.json()["models"][0]["status"] == "error"


@pytest.mark.asyncio
@respx.mock
async def test_a_name_that_is_not_a_model_id_is_an_invalid_name_result_and_no_request(client) -> None:
    r = await client.post(ROUTE, json={"models": ["../../api/whoami"]})

    assert r.status_code == 200, r.text
    assert r.json()["models"][0]["status"] == "invalid_name"
    assert respx.calls.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"models": []}, {}, {"models": "BAAI/bge-small-en-v1.5"}, {"models": [1]}])
async def test_a_body_without_a_list_of_names_is_422(client, body: dict) -> None:
    r = await client.post(ROUTE, json=body)

    assert r.status_code == 422, r.text


@pytest.mark.asyncio
@respx.mock
async def test_50_models_is_the_most_and_51_is_422(client) -> None:
    names = [f"org/model-{i}" for i in range(51)]
    for name in names[:50]:
        respx.get(_url(name)).mock(return_value=httpx.Response(200))

    ok = await client.post(ROUTE, json={"models": names[:50]})
    too_many = await client.post(ROUTE, json={"models": names})

    assert ok.status_code == 200 and len(ok.json()["models"]) == 50, ok.text
    assert too_many.status_code == 422, too_many.text


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["user", "restricted"])
async def test_a_role_user_is_refused(raw_client, app, role: str) -> None:
    """System configuration is admin only: the route is mounted with the admin gate, like every provider helper."""
    from tests.api.test_require_user_admin import _login, _seed

    await _seed(app, uid=f"u-{role}", username=role, role=role)
    await _login(raw_client, role)

    r = await raw_client.post(ROUTE, json={"models": [GOOD]})

    assert r.status_code == 403, r.text
    assert r.json()["extensions"]["error"] == "forbidden_role"


@pytest.mark.asyncio
async def test_an_anonymous_caller_is_refused(raw_client) -> None:
    r = await raw_client.post(ROUTE, json={"models": [GOOD]})

    assert r.status_code == 401, r.text
    assert r.json()["extensions"]["error"] == "auth_required"


@pytest.mark.asyncio
@respx.mock
async def test_the_token_is_not_echoed(client) -> None:
    """Not in the answer to a check that worked, one the Hub refused, or one that failed on the network; and not in the 422 of a bad body either."""
    respx.get(_url(GOOD)).mock(side_effect=httpx.ConnectError(f"Illegal header value b'Bearer {TOKEN}'"))
    respx.get(_url(MISSING)).mock(return_value=httpx.Response(401, headers={"X-Error-Message": "Invalid credentials in Authorization header"}))

    answered = await client.post(ROUTE, json={"models": [GOOD, MISSING], "token": TOKEN})
    refused = await client.post(ROUTE, json={"models": [], "token": TOKEN})

    assert answered.status_code == 200 and refused.status_code == 422
    assert TOKEN not in answered.text and TOKEN not in refused.text


@pytest.mark.asyncio
async def test_the_route_is_published_in_the_openapi_schema(app) -> None:
    operation = app.openapi()["paths"][ROUTE]["post"]

    assert operation["tags"] == ["providers"] and operation["summary"]
