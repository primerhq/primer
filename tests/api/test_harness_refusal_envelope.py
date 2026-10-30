"""G15-03 (LOW): harness operation refusals must render as RFC 7807 problem+json.

At the G15 baseline (main 8179ccd63) 23 refusal sites in
``primer/api/routers/harness.py`` answered with a raw
``JSONResponse(status_code=N, content={"code": ..., "detail": ...})``:
application/json, no type URI, no title, no request id, and the machine
code sitting at the top level of the body. The fix routes every refusal
through ``raise HTTPException(status_code=N, detail={"code": ...,
"detail": ...})`` so the shared problem handler (primer/api/errors.py)
renders the one envelope shape: ``type``/``title``/``status``/
``instance``, the sentence preserved as ``detail``
(``_detail_from_mapping`` prefers the ``detail`` key), and the machine
code kept under ``extensions`` (plus the threaded ``request_id``).

These pins drive three representatives of the 23 (the
``tracked_entities_on_inbound`` 422, an outbound ``direction_mismatch``
409, and a non-admin scope refusal 403) and pin the envelope shape; the
rest of the 23 is covered by the existing route tests (their old-shape
asserts are flipped in the same PR - see the G15-B implementation
report).

Audit: /var/tmp/revamp/audit/G15.md (G15-03); the canonical 23-site list
is probe_g15_03_harness_envelope_bypass.py's output. Polarity: RED at the
base commit, GREEN once the fix lands. The 403 pin is already green at
the base (that site raises HTTPException, not JSONResponse): it guards
the envelope shape for the auth/scope refusal class against regression.
"""

from __future__ import annotations

from datetime import datetime, timezone

from primer.auth.passwords import hash_password
from primer.model.user import User

from tests.api.conftest import app, client, raw_client  # noqa: F401


PROBLEM_JSON = "application/problem+json"


def _assert_problem(r, *, status: int, type_uri: str, title: str, instance: str) -> dict:
    """Assert the RFC 7807 envelope shape on ``r`` and return its body."""
    assert r.status_code == status
    assert r.headers["content-type"].startswith(PROBLEM_JSON), (
        f"expected {PROBLEM_JSON}, got {r.headers.get('content-type')!r}"
    )
    body = r.json()
    assert body["type"] == type_uri
    assert body["title"] == title
    assert body["status"] == status
    assert body["instance"] == instance
    assert isinstance(body["detail"], str) and body["detail"]
    # The test app installs the request-id middleware, so the envelope
    # must thread the request id into extensions (the console's "Copy
    # request id" works on the refusal path).
    assert "request_id" in body["extensions"]
    return body


async def _seed_user(app, *, uid: str, username: str, role: str) -> None:
    """Seed a user directly via storage (bypassing single-user register)."""
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


async def test_tracked_entities_on_inbound_422_is_a_problem_envelope(client):
    r = await client.post(
        "/v1/harnesses",
        json={
            "name": "Inbound",
            "slug": "g15b-inbound",
            "git_url": "https://github.com/example/repo",
            "direction": "inbound",
            "tracked_entities": [
                {"kind": "agent", "source_id": "agx_demo", "template_name": "alpha"},
            ],
        },
    )
    body = _assert_problem(
        r,
        status=422,
        type_uri="/errors/validation-error",
        title="Validation Error",
        instance="/v1/harnesses",
    )
    assert body["extensions"]["code"] == "tracked_entities_on_inbound"
    assert "may only be supplied" in body["detail"]


async def test_fetch_on_outbound_409_is_a_problem_envelope(client):
    r = await client.post(
        "/v1/harnesses",
        json={
            "name": "Outbound",
            "slug": "g15b-outbound",
            "git_url": "https://github.com/example/repo",
            "direction": "outbound",
            "tracked_entities": [
                {"kind": "agent", "source_id": "agx_demo", "template_name": "alpha"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    hid = r.json()["id"]
    r2 = await client.post(f"/v1/harnesses/{hid}/fetch")
    body = _assert_problem(
        r2,
        status=409,
        type_uri="/errors/conflict",
        title="Conflict",
        instance=f"/v1/harnesses/{hid}/fetch",
    )
    assert body["extensions"]["code"] == "direction_mismatch"
    assert "fetch is an inbound operation" in body["detail"]


async def test_non_admin_scope_refusal_403_is_a_problem_envelope(app, raw_client):
    await _seed_user(app, uid="g15b-user-1", username="g15b-user", role="user")
    raw_client.cookies.clear()
    login = await raw_client.post(
        "/v1/auth/login", json={"username": "g15b-user", "password": "pw"}
    )
    assert login.status_code == 200, login.text
    r = await raw_client.post(
        "/v1/harnesses",
        json={
            "name": "Inbound",
            "slug": "g15b-scope",
            "git_url": "https://github.com/example/repo",
        },
    )
    body = _assert_problem(
        r,
        status=403,
        type_uri="/errors/forbidden",
        title="Forbidden",
        instance="/v1/harnesses",
    )
    # This mapping carries the code under "error" (the auth gate's
    # vocabulary), not "code".
    assert body["extensions"]["error"] == "forbidden_role"
    assert "admin role" in body["detail"]
