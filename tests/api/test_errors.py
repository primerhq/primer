"""Unit tests for primer.api.errors — RFC 7807 problem+json mapping."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from primer.api.errors import (
    PROBLEM_JSON_MEDIA_TYPE,
    ProblemDetails,
    common_responses,
    register_error_handlers,
)
from primer.model.except_ import (
    AuthenticationError,
    AuthRequiredError,
    BadRequestError,
    ConfigError,
    ConflictError,
    DimensionMismatchError,
    PrimerError,
    ModelNotFoundError,
    NetworkError,
    NotFoundError,
    ProviderError,
    RateLimitError,
    ServerError,
    UnsupportedContentError,
)


def _make_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    return app


def _mount_raiser(app: FastAPI, path: str, exc: Exception) -> None:
    @app.get(path)
    def _raise() -> None:
        raise exc


class TestProblemDetails:
    def test_required_fields(self) -> None:
        p = ProblemDetails(
            type="/errors/not-found",
            title="Not Found",
            status=404,
            detail="agent 'foo' does not exist",
        )
        assert p.instance is None
        assert p.extensions is None

    def test_with_extensions(self) -> None:
        p = ProblemDetails(
            type="/errors/auth-required",
            title="Auth Required",
            status=401,
            detail="oauth needed",
            extensions={"auth_url": "https://example.com/oauth"},
        )
        assert p.extensions == {"auth_url": "https://example.com/oauth"}


@pytest.mark.parametrize(
    "exc, expected_status, expected_type_suffix",
    [
        (BadRequestError("bad"), 400, "/errors/bad-request"),
        (AuthenticationError("bad creds"), 401, "/errors/authentication-failed"),
        (NotFoundError("missing"), 404, "/errors/not-found"),
        (ModelNotFoundError("no such model"), 404, "/errors/model-not-found"),
        (ConflictError("dup id"), 409, "/errors/conflict"),
        (RateLimitError("rate limited"), 429, "/errors/rate-limited"),
        (
            DimensionMismatchError(
                "dim mismatch",
                embedder_dim=384,
                collection_dim=768,
                collection_id="col-1",
            ),
            422,
            "/errors/dimension-mismatch",
        ),
        (UnsupportedContentError("nope"), 422, "/errors/unsupported-content"),
        (ConfigError("bad setup"), 503, "/errors/service-unavailable"),
        (ServerError("upstream 5xx"), 502, "/errors/provider-server-error"),
        (ProviderError("provider failed"), 502, "/errors/provider-error"),
        (NetworkError("dns failed"), 504, "/errors/network-error"),
        (PrimerError("generic"), 500, "/errors/internal"),
    ],
)
def test_primer_error_maps_to_problem_details(
    exc: PrimerError, expected_status: int, expected_type_suffix: str
) -> None:
    app = _make_app()
    _mount_raiser(app, "/raise", exc)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/raise")
    assert response.status_code == expected_status
    assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    body = response.json()
    assert body["type"] == expected_type_suffix
    assert body["status"] == expected_status
    assert "title" in body and body["title"]
    assert "detail" in body and body["detail"]
    assert body["instance"] == "/raise"


def test_config_error_maps_to_503() -> None:
    """ConfigError should map to 503 (service unavailable / not ready).

    See spec docs/superpowers/specs/2026-05-10-background-execution-scheduler-design.md §12.
    """
    app = _make_app()
    _mount_raiser(app, "/raise", ConfigError("scheduler not configured"))
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/raise")
    assert response.status_code == 503
    assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    body = response.json()
    assert body["type"] == "/errors/service-unavailable"
    assert body["title"] == "Service Unavailable"
    assert body["status"] == 503
    assert body["detail"] == "scheduler not configured"
    assert body["instance"] == "/raise"


def test_auth_required_carries_auth_url_extension() -> None:
    app = _make_app()
    _mount_raiser(
        app,
        "/raise",
        AuthRequiredError(
            "oauth needed",
            auth_url="https://oauth.example/authorize",
            state="opaque",
        ),
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/raise")
    assert response.status_code == 401
    body = response.json()
    assert body["type"] == "/errors/auth-required"
    assert body["extensions"]["auth_url"] == "https://oauth.example/authorize"


def test_pydantic_validation_error_returns_422_with_errors_extension() -> None:
    app = _make_app()

    class _Body(BaseModel):
        x: int

    @app.post("/echo")
    def _echo(body: _Body) -> dict:
        return {"x": body.x}

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/echo", json={"x": "not-an-int"})
    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    body = response.json()
    assert body["type"] == "/errors/validation-error"
    assert "errors" in body["extensions"]
    assert isinstance(body["extensions"]["errors"], list)
    assert len(body["extensions"]["errors"]) >= 1


@pytest.mark.asyncio
async def test_bare_exception_returns_500_internal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Use httpx AsyncClient because TestClient re-raises base Exception
    even with raise_server_exceptions=False in this Starlette version."""
    import httpx
    from httpx import ASGITransport

    app = _make_app()
    _mount_raiser(app, "/raise", RuntimeError("boom"))
    with caplog.at_level(logging.ERROR):
        # ServerErrorMiddleware re-raises after invoking our handler so
        # the ASGI server can log it; suppress the re-raise here.
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.get("/raise")
    assert response.status_code == 500
    body = response.json()
    assert body["type"] == "/errors/internal"
    # No stack/exception type leaked into the response body.
    assert "RuntimeError" not in body["detail"]
    # But the stack IS logged. ``logger.exception`` puts the exception
    # in ``record.exc_info``; the formatted message doesn't carry it.
    assert any(
        r.exc_info is not None and "boom" in str(r.exc_info[1])
        for r in caplog.records
    )


class TestStarletteHTTPException:
    """Raw ``HTTPException`` (~91 raise sites, incl. every require_auth /
    require_user / require_admin / require_scope rejection) must render the
    same RFC 7807 envelope as PrimerError, not FastAPI's default
    ``{"detail": ...}`` as plain application/json. See
    docs/dev/architecture/rest-api.md: "Every error is
    application/problem+json".
    """

    def test_string_detail_renders_problem_envelope(self) -> None:
        app = _make_app()
        _mount_raiser(app, "/raise", HTTPException(status_code=404, detail="nope"))
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/raise")
        assert response.status_code == 404
        assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
        body = response.json()
        assert body["type"] == "/errors/not-found"
        assert body["title"] == "Not Found"
        assert body["status"] == 404
        assert body["detail"] == "nope"
        assert body["instance"] == "/raise"

    def test_auth_401_is_problem_json(self) -> None:
        app = _make_app()
        _mount_raiser(
            app,
            "/raise",
            HTTPException(status_code=401, detail={"error": "auth_required"}),
        )
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/raise")
        assert response.status_code == 401
        assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
        body = response.json()
        assert body["type"] == "/errors/authentication-failed"
        assert body["status"] == 401
        # The machine-readable key survives as an extension member.
        assert body["extensions"]["error"] == "auth_required"

    def test_forbidden_403_is_problem_json(self) -> None:
        app = _make_app()
        _mount_raiser(
            app,
            "/raise",
            HTTPException(status_code=403, detail={"error": "forbidden_role"}),
        )
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/raise")
        assert response.status_code == 403
        assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
        body = response.json()
        assert body["type"] == "/errors/forbidden"
        assert body["title"] == "Forbidden"
        assert body["status"] == 403
        assert body["extensions"]["error"] == "forbidden_role"

    def test_dict_detail_conveys_code_and_extra_keys(self) -> None:
        app = _make_app()
        _mount_raiser(
            app,
            "/raise",
            HTTPException(
                status_code=403,
                detail={"code": "scope_required", "scope": "mcp"},
            ),
        )
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/raise")
        body = response.json()
        assert body["extensions"]["code"] == "scope_required"
        assert body["extensions"]["scope"] == "mcp"
        # detail is a string per RFC 7807 -- never a stringified dict.
        assert isinstance(body["detail"], str)
        assert "{" not in body["detail"]

    def test_dict_detail_message_becomes_detail(self) -> None:
        app = _make_app()
        _mount_raiser(
            app,
            "/raise",
            HTTPException(
                status_code=501,
                detail={"error": "not_implemented", "message": "pause is not supported"},
            ),
        )
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/raise")
        body = response.json()
        assert body["detail"] == "pause is not supported"
        assert body["extensions"]["error"] == "not_implemented"

    def test_headers_survive(self) -> None:
        app = _make_app()
        _mount_raiser(
            app,
            "/raise",
            HTTPException(
                status_code=401,
                detail="token expired",
                headers={"WWW-Authenticate": 'Bearer realm="primer"'},
            ),
        )
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/raise")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == 'Bearer realm="primer"'
        assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE

    def test_unknown_route_returns_problem_json_404(self) -> None:
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/no-such-route")
        assert response.status_code == 404
        assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
        body = response.json()
        assert body["type"] == "/errors/not-found"
        assert body["status"] == 404

    def test_bodyless_status_stays_bodyless(self) -> None:
        # 204/304 MUST NOT carry a body; a problem envelope there would be
        # a protocol violation.
        app = _make_app()
        _mount_raiser(app, "/raise", HTTPException(status_code=304))
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/raise")
        assert response.status_code == 304
        assert not response.content


class TestCommonResponses:
    def test_returns_problem_details_model_for_each_code(self) -> None:
        responses = common_responses(404, 409)
        assert 404 in responses
        assert 409 in responses
        for _code, schema in responses.items():
            assert schema["model"] is ProblemDetails

    def test_unknown_code_raises(self) -> None:
        with pytest.raises(KeyError):
            common_responses(418)
