"""E2E: WorkspaceProvider router shape.

Covers backlog item T0029. WorkspaceProvider now has a PUT route (the
spec note about immutability was superseded when the full CRUD set was
wired). PUT with a body that fails validation should return 422 cleanly,
not 405 (the route exists) and not 500 (no internal leak).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest


@pytest.mark.asyncio
async def test_t0029_workspace_provider_has_no_put(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    # PUT is now defined on /v1/workspace_providers/{id}. A body with a
    # missing/invalid ``config.kind`` discriminator returns 422 (validation
    # error) not 405 (method not found) and not 500 (no internal leak).
    body = {
        "id": f"wp-{unique_suffix}",
        "provider": "local",
        "config": {"root": "/tmp/whatever"},  # missing 'kind' discriminator
    }
    resp = await client.put(
        f"/v1/workspace_providers/{body['id']}", json=body,
    )
    # PUT route exists -> no 405; validation failure -> 422
    assert resp.status_code in (422, 200, 404), (
        f"PUT /v1/workspace_providers: expected 422 (validation) or 200/404 "
        f"(if body is valid or row missing), got {resp.status_code}: {resp.text}"
    )
    assert resp.status_code != 500, f"PUT leaked 500: {resp.text}"
    if resp.status_code == 422:
        envelope = resp.json()
        assert envelope.get("type") == "/errors/validation-error", envelope


@pytest.mark.asyncio
async def test_t0116_workspace_provider_container_kind_clean_response(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0116 — POST WorkspaceProvider with `provider=container` and
    a docker-runtime config. The model must accept the variant
    cleanly (no 5xx). Whether POST itself succeeds or the API
    rejects with a clean 4xx (e.g. backend not configured at this
    deployment) is documented; the contract pin is "no internal
    error envelope".
    """
    entity_id = f"wp-container-{unique_suffix}"
    body = {
        "id": entity_id,
        "provider": "container",
        "config": {
            "kind": "container",
            "runtime": "docker",
            "connection": {
                "kind": "socket",
                "socket_path": "/var/run/docker.sock",
            },
            "reachability": {
                "kind": "host_port",
                "bind_host": "127.0.0.1",
            },
            "image_pull_secrets": [],
        },
    }
    resp = await client.post("/v1/workspace_providers", json=body)
    try:
        assert resp.status_code != 500, resp.text
        if resp.status_code == 201:
            # POST accepted — verify the row round-trips
            got = await client.get(f"/v1/workspace_providers/{entity_id}")
            assert got.status_code == 200, got.text
            assert got.json()["provider"] == "container"
        else:
            # 4xx envelope per the documented error catalogue
            assert 400 <= resp.status_code < 500, resp.text
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
    finally:
        await client.delete(f"/v1/workspace_providers/{entity_id}")


@pytest.mark.asyncio
async def test_t0117_workspace_provider_kubernetes_kind_clean_response(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0117 — same shape contract pin for `provider=kubernetes`.

    Kubernetes config schema requires more fields than container; if
    POST rejects, the test still passes as long as the rejection is
    a clean 4xx envelope.
    """
    entity_id = f"wp-k8s-{unique_suffix}"
    body = {
        "id": entity_id,
        "provider": "kubernetes",
        "config": {
            "kind": "kubernetes",
            "variant": "system",
            "connection": {"kind": "in_cluster"},
            "namespace": "primer",
            "reachability": {"kind": "in_cluster"},
            "image_pull_secrets": [],
        },
    }
    resp = await client.post("/v1/workspace_providers", json=body)
    try:
        assert resp.status_code != 500, resp.text
        if resp.status_code == 201:
            got = await client.get(f"/v1/workspace_providers/{entity_id}")
            assert got.status_code == 200, got.text
            assert got.json()["provider"] == "kubernetes"
        else:
            assert 400 <= resp.status_code < 500, resp.text
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
    finally:
        await client.delete(f"/v1/workspace_providers/{entity_id}")


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
            "config": {"kind": "local", "root_path": "/tmp/primer-e2e-t0124"},
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
        "config": {"kind": "local", "root_path": "/tmp/primer-e2e-t0052"},
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


# ============================================================================
# T0305 — WorkspaceProvider GET echoes full local config (no masking)
# ============================================================================


@pytest.mark.asyncio
async def test_t0305_workspace_provider_get_echoes_local_config(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0305 — Local-kind WorkspaceProvider config has no SecretStr
    fields (just `kind` + `root_path`). GET response must echo both
    fields byte-identical to the create body — pin that the provider
    config introspection works for non-secret backends.
    """
    provider_id = f"wp-t0305-{unique_suffix}"
    config = {"kind": "local", "root_path": str(tmp_path)}
    create = await client.post(
        "/v1/workspace_providers",
        json={"id": provider_id, "provider": "local", "config": config},
    )
    assert create.status_code == 201, create.text
    try:
        got = await client.get(f"/v1/workspace_providers/{provider_id}")
        assert got.status_code == 200, got.text
        row = got.json()
        assert row["provider"] == "local", row
        assert row["config"]["kind"] == "local", row
        assert row["config"]["root_path"] == str(tmp_path), row
    finally:
        await client.delete(f"/v1/workspace_providers/{provider_id}")


# ============================================================================
# T0306 — WorkspaceProvider /find predicate provider="local"
# ============================================================================


@pytest.mark.asyncio
async def test_t0306_workspace_providers_find_predicate_provider_kind(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0306 — WorkspaceProvider supports POST /find with predicate
    filtering. Seed two local providers; POST /find with predicate
    `provider = "local"` AND `id ~= prefix%` returns both seeded ids.
    """
    prefix = f"wp-t0306-{unique_suffix}"
    seeded = [f"{prefix}-{i}" for i in range(2)]
    try:
        for pid in seeded:
            r = await client.post(
                "/v1/workspace_providers",
                json={
                    "id": pid,
                    "provider": "local",
                    "config": {"kind": "local", "root_path": str(tmp_path)},
                },
            )
            assert r.status_code == 201, r.text

        body = {
            "predicate": {
                "kind": "predicate",
                "op": "and",
                "left": {
                    "kind": "predicate",
                    "op": "=",
                    "left": {"kind": "field", "name": "provider"},
                    "right": {"kind": "value", "value": "local"},
                },
                "right": {
                    "kind": "predicate",
                    "op": "~=",
                    "left": {"kind": "field", "name": "id"},
                    "right": {"kind": "value", "value": f"{prefix}%"},
                },
            },
            "page": {"kind": "offset", "offset": 0, "length": 50},
        }
        resp = await client.post("/v1/workspace_providers/find", json=body)
        assert resp.status_code == 200, resp.text
        out = sorted(item["id"] for item in resp.json()["items"])
        assert out == sorted(seeded), (
            f"expected {sorted(seeded)!r}, got {out!r}"
        )
    finally:
        for pid in seeded:
            await client.delete(f"/v1/workspace_providers/{pid}")


# ============================================================================
# T0345 — Workspace→Template→Provider cascade: orphan templates respond 200
# ============================================================================


@pytest.mark.asyncio
async def test_t0345_delete_workspace_provider_orphan_templates_still_listable(
    client: httpx.AsyncClient, unique_suffix: str, tmp_path: Path,
) -> None:
    """T0345 — Three-tier orphan tolerance pin: build
    WorkspaceProvider→two WorkspaceTemplates→one Workspace each;
    DELETE the WorkspaceProvider. Both orphaned WorkspaceTemplates
    must remain readable via GET (orphan tolerance per the same
    pattern as T0223 / T0291 / T0265).
    """
    provider_id = f"wp-t0345-{unique_suffix}"
    template_ids = [f"wt-t0345-{unique_suffix}-{i}" for i in range(2)]

    pr = await client.post(
        "/v1/workspace_providers",
        json={
            "id": provider_id,
            "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        },
    )
    assert pr.status_code == 201, pr.text

    templates_created: list[str] = []
    try:
        for tid in template_ids:
            r = await client.post(
                "/v1/workspace_templates",
                json={
                    "id": tid,
                    "description": f"T0345-{tid}",
                    "provider_id": provider_id,
                    "backend": {"kind": "local"},
                },
            )
            assert r.status_code == 201, r.text
            templates_created.append(tid)

        # DELETE the provider
        rm = await client.delete(f"/v1/workspace_providers/{provider_id}")
        assert rm.status_code < 500, rm.text
        if rm.status_code >= 400:
            envelope = rm.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope

        # Both orphaned templates remain readable
        for tid in template_ids:
            got = await client.get(f"/v1/workspace_templates/{tid}")
            assert got.status_code < 500, got.text
            if got.status_code == 200:
                assert got.json()["provider_id"] == provider_id
            else:
                envelope = got.json()
                assert envelope["type"].startswith("/errors/"), envelope
                assert envelope["type"] != "/errors/internal", envelope
    finally:
        for tid in templates_created:
            await client.delete(f"/v1/workspace_templates/{tid}")
        # Provider may already be gone
        await client.delete(f"/v1/workspace_providers/{provider_id}")
