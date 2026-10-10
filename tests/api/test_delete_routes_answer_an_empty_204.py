"""A DELETE that answers 204 has no body (board task 01a12350-941b, found in the ui-e2e server log of run 37988249822 attempt 3).

Five routes returned ``JSONResponse(status_code=204, content=None)``. Starlette renders ``None`` as the four bytes ``null`` and gives a 204 no ``content-length``, so uvicorn expects ZERO body bytes and raises
``Response content longer than Content-Length``: an ERROR logged for every such delete (``unhandled exception in API request``) and a keep-alive connection that is broken for the client's next request. The client
already has the 204, so a test of the status alone passes. The in-process client does not run uvicorn's check, so these cases assert the BODY, which is where the defect is: it must be empty. A static scan keeps
the shape from coming back.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from primer.auth.passwords import hash_password
from primer.model.api_token import ApiToken
from primer.model.user import User

ROOT = Path(__file__).resolve().parents[2]


def _delayed_trigger(slug: str) -> dict:
    return {"slug": slug, "name": slug, "config": {"kind": "delayed", "fire_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}}


def _assert_empty_204(response) -> None:
    assert response.status_code == 204, response.text
    assert response.content == b"", f"a 204 must have no body; this one has {response.content!r}"
    # A 204 carries no content-length (RFC 9110): absent, or 0; never the 4 of 'null'.
    assert response.headers.get("content-length", "0") == "0", response.headers.get("content-length")


@pytest.mark.asyncio
async def test_delete_trigger_answers_an_empty_204(client) -> None:
    created = await client.post("/v1/triggers", json=_delayed_trigger("empty-204-trigger"))
    assert created.status_code == 201, created.text

    _assert_empty_204(await client.delete(f"/v1/triggers/{created.json()['id']}"))


@pytest.mark.asyncio
async def test_delete_subscription_answers_an_empty_204(client) -> None:
    created = await client.post("/v1/triggers", json=_delayed_trigger("empty-204-subscription"))
    assert created.status_code == 201, created.text
    trigger_id = created.json()["id"]
    sub = await client.post(
        f"/v1/triggers/{trigger_id}/subscriptions",
        json={"config": {"kind": "session_append", "session_id": "sess-1"}, "payload_template": None, "parallelism": "skip"},
    )
    assert sub.status_code == 201, sub.text

    _assert_empty_204(await client.delete(f"/v1/triggers/{trigger_id}/subscriptions/{sub.json()['id']}"))


@pytest.mark.asyncio
async def test_revoke_api_token_answers_an_empty_204(client) -> None:
    created = await client.post("/v1/auth/tokens", json={"name": "empty-204", "scopes": ["mcp"]})
    assert created.status_code == 201, created.text

    _assert_empty_204(await client.delete(f"/v1/auth/tokens/{created.json()['id']}"))


@pytest.mark.asyncio
async def test_admin_revoke_of_a_users_token_answers_an_empty_204(raw_client, app) -> None:
    await raw_client.post("/v1/auth/register", json={"username": "boss", "password": "supersecret"})
    sp = app.state.storage_provider
    await sp.get_storage(User).create(
        User(id="user-bob", username="bob", role="user", password_hash=await hash_password("supersecret"), created_at=datetime.now(timezone.utc))
    )
    await sp.get_storage(ApiToken).create(
        ApiToken(id="at-1", user_id="user-bob", name="bob-key", token_hash="a" * 64, prefix="pk_abcde", scopes=["mcp"], created_at=datetime.now(timezone.utc))
    )

    _assert_empty_204(await raw_client.delete("/v1/admin/users/user-bob/tokens/at-1"))


# ---- the static guard -------------------------------------------------------------------------------------------------------------------------------


def _json_204_calls(source: str) -> list[int]:
    """Line numbers of every ``JSONResponse(...)`` call that is given ``status_code=204`` (any argument order, any line layout)."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and (getattr(node.func, "id", None) == "JSONResponse" or getattr(node.func, "attr", None) == "JSONResponse"):
            if any(kw.arg == "status_code" and isinstance(kw.value, ast.Constant) and kw.value.value == 204 for kw in node.keywords):
                lines.append(node.lineno)
    return lines


def test_the_scan_sees_every_layout_of_a_json_204() -> None:
    for source in (
        "return JSONResponse(status_code=204, content=None)",
        "return JSONResponse(content=None, status_code=204)",
        "return JSONResponse(\n    status_code=204,\n    content=None,\n)",
        "return responses.JSONResponse(status_code=204)",
    ):
        assert _json_204_calls(source), source
    for source in ("return Response(status_code=204)", "return JSONResponse(status_code=200, content={})", "return JSONResponse(status_code=status)"):
        assert not _json_204_calls(source), source


def test_no_route_answers_a_204_with_a_json_body() -> None:
    offenders = [f"{path.relative_to(ROOT)}:{line}" for path in sorted((ROOT / "primer").rglob("*.py")) for line in _json_204_calls(path.read_text(encoding="utf-8"))]
    assert offenders == [], f"a 204 built as a JSONResponse carries the body 'null' and uvicorn refuses it; answer Response(status_code=204): {offenders}"
