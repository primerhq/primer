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
