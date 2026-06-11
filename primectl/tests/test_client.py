import httpx
import pytest

from primectl.client import ApiClient, ApiError, ConnectionFailed


def _client(handler, token=None):
    transport = httpx.MockTransport(handler)
    return ApiClient(server="http://test", token=token, transport=transport)


def test_get_returns_json():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/agents"
        return httpx.Response(200, json={"kind": "offset", "items": [{"id": "a"}]})

    c = _client(handler)
    resp = c.request("get", "/v1/agents")
    assert resp.json()["items"] == [{"id": "a"}]


def test_token_sets_bearer_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    c = _client(handler, token="tok123")
    c.request("get", "/v1/agents")
    assert seen["auth"] == "Bearer tok123"


def test_no_token_omits_auth_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    c = _client(handler)
    c.request("get", "/v1/agents")
    assert seen["auth"] is None


def test_4xx_raises_api_error_with_problem_details():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"type": "not-found", "title": "Not Found",
                  "status": 404, "detail": "Agent 'x' does not exist"},
        )

    c = _client(handler)
    with pytest.raises(ApiError) as exc:
        c.request("get", "/v1/agents/x")
    assert exc.value.status == 404
    assert exc.value.problem["detail"] == "Agent 'x' does not exist"


def test_transport_error_raises_connection_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    c = _client(handler)
    with pytest.raises(ConnectionFailed):
        c.request("get", "/v1/agents")
