"""E2E: WorkspaceProvider router shape — no PUT method.

Covers backlog item T0029. Spec §12 says WorkspaceProvider has CRUD
**with no `PUT`** because providers are immutable once created. The
absence of the method must surface as 405 Method Not Allowed, not as
a generic 422 or 404.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_t0029_workspace_provider_has_no_put(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    # Whether the id exists or not is irrelevant — PUT isn't routed at
    # all for /v1/workspace_providers, so FastAPI's default 405 handler
    # should answer.
    body = {
        "id": f"wp-{unique_suffix}",
        "provider": "local",
        "config": {"root": "/tmp/whatever"},
    }
    resp = await client.put(
        f"/v1/workspace_providers/{body['id']}", json=body,
    )
    assert resp.status_code == 405, (
        f"expected 405 Method Not Allowed (PUT not defined on this router), "
        f"got {resp.status_code}: {resp.text}"
    )
    # FastAPI/Starlette's default 405 response includes an `allow` header
    # listing the methods that ARE defined on the path.
    allow = resp.headers.get("allow", "").upper()
    assert allow, "405 response should carry an 'Allow' header"
    assert "PUT" not in allow.split(", "), (
        f"PUT should not appear in Allow header: {allow!r}"
    )


@pytest.mark.asyncio
async def test_t0124_workspace_template_description_optional(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0124 — pin the actual contract: is `description` required on
    WorkspaceTemplate? Either the POST succeeds (description optional)
    or it 422s with /errors/validation-error (description required).
    Both are clean contracts; the test records the actual behaviour
    so future schema changes are caught.
    """
    provider_id = f"wp-tpl-desc-{unique_suffix}"
    template_id = f"wt-no-desc-{unique_suffix}"

    pr = await client.post(
        "/v1/workspace_providers",
        json={
            "id": provider_id,
            "provider": "local",
            "config": {"kind": "local", "path": "/tmp/matrix-e2e-t0124"},
        },
    )
    assert pr.status_code == 201, pr.text

    try:
        # POST WITHOUT a description field
        body_no_desc = {
            "id": template_id,
            "provider_id": provider_id,
            "backend": {"kind": "local"},
        }
        resp = await client.post(
            "/v1/workspace_templates", json=body_no_desc,
        )
        if resp.status_code == 201:
            # Description is optional. Verify it round-trips as
            # null/empty/missing without surprise.
            try:
                got = await client.get(
                    f"/v1/workspace_templates/{template_id}",
                )
                assert got.status_code == 200, got.text
                # The field may be absent, null, or default ""
                desc = got.json().get("description")
                assert desc in (None, ""), (
                    f"unexpected description on no-desc template: {desc!r}"
                )
            finally:
                await client.delete(f"/v1/workspace_templates/{template_id}")
        else:
            # Description is required — must be a clean 422 envelope
            assert resp.status_code == 422, resp.text
            envelope = resp.json()
            assert envelope["type"] == "/errors/validation-error", envelope
            assert envelope["status"] == 422
    finally:
        await client.delete(f"/v1/workspace_providers/{provider_id}")


@pytest.mark.asyncio
async def test_t0052_delete_workspace_provider_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0052 — POST WorkspaceProvider, DELETE, GET = 404 with the
    /errors/not-found envelope. Mirrors the standard CRUD-delete
    contract (T0009) for the immutable-by-design WorkspaceProvider.
    """
    entity_id = f"wp-del-{unique_suffix}"
    body = {
        "id": entity_id,
        "provider": "local",
        "config": {"kind": "local", "path": "/tmp/matrix-e2e-t0052"},
    }
    create = await client.post("/v1/workspace_providers", json=body)
    assert create.status_code == 201, create.text

    rm = await client.delete(f"/v1/workspace_providers/{entity_id}")
    assert rm.status_code == 204, rm.text

    gone = await client.get(f"/v1/workspace_providers/{entity_id}")
    assert gone.status_code == 404, gone.text
    envelope = gone.json()
    assert envelope["type"] == "/errors/not-found", envelope
    assert envelope["status"] == 404
