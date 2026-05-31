"""Confirm new dependency-related error codes surface via GET /v1/harnesses/{id}.

Plan A §8 introduced these error codes that the dispatch layer can stamp
onto ``Harness.last_operation_error`` when an operation fails:

- ``dependency_cycle``           — cycle detected in transitive walk
- ``dependency_version_conflict`` — same slug pinned to divergent refs
- ``dependency_fetch_failed``     — git error sub-fetching a dep
- ``dependency_yaml_invalid``     — sub harness.yaml malformed / missing fields
- ``apply_id_conflict``           — resolved id already owned by another harness

The Harness REST API is async (FETCH/INSTALL/SYNC return 202 and the
worker stamps the result onto the row); these codes therefore surface
on the GET response via the structured JSON in ``last_operation_error``.
These tests stand up a Harness row in ERROR state with each code and
confirm the GET response round-trips the structured payload verbatim
with HTTP 200.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.model.harness import Harness, HarnessStatus


def _make_error_harness(
    harness_id: str,
    *,
    last_operation_error: str,
) -> Harness:
    return Harness(
        id=harness_id,
        slug="err-harness-" + harness_id[-4:],
        name="Errored Harness",
        git_url="https://github.com/example/repo",
        status=HarnessStatus.ERROR,
        created_at=datetime.now(timezone.utc),
        last_operation_error=last_operation_error,
    )


@pytest.mark.asyncio
async def test_get_returns_dependency_cycle_in_last_operation_error(
    client, fake_storage_provider,
):
    storage = fake_storage_provider.get_storage(Harness)
    err = {
        "code": "dependency_cycle",
        "message": "dependency cycle: a -> b -> a",
        "path": ["a", "b", "a"],
    }
    h = _make_error_harness(
        "hns_errdcycle01", last_operation_error=json.dumps(err),
    )
    await storage.create(h)

    resp = await client.get(f"/v1/harnesses/{h.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["last_operation_error"] is not None
    payload = json.loads(body["last_operation_error"])
    assert payload["code"] == "dependency_cycle"
    assert payload["path"] == ["a", "b", "a"]


@pytest.mark.asyncio
async def test_get_returns_dependency_version_conflict_in_last_operation_error(
    client, fake_storage_provider,
):
    storage = fake_storage_provider.get_storage(Harness)
    err = {
        "code": "dependency_version_conflict",
        "message": "dependency version conflict for 'c'",
        "slug": "c",
        "ref_a": "v1",
        "ref_b": "v2",
        "path_a": ["A"],
        "path_b": ["B"],
    }
    h = _make_error_harness(
        "hns_errvconf01", last_operation_error=json.dumps(err),
    )
    await storage.create(h)

    resp = await client.get(f"/v1/harnesses/{h.id}")
    assert resp.status_code == 200
    body = resp.json()
    payload = json.loads(body["last_operation_error"])
    assert payload["code"] == "dependency_version_conflict"
    assert payload["slug"] == "c"
    assert payload["ref_a"] == "v1"
    assert payload["ref_b"] == "v2"


@pytest.mark.asyncio
async def test_get_returns_dependency_fetch_failed_in_last_operation_error(
    client, fake_storage_provider,
):
    storage = fake_storage_provider.get_storage(Harness)
    err = {
        "code": "dependency_fetch_failed",
        "message": "git: ref not found",
        "git_url": "https://github.com/example/missing",
        "ref": "main",
        "inner_code": "ref_not_found",
    }
    h = _make_error_harness(
        "hns_errfetch01", last_operation_error=json.dumps(err),
    )
    await storage.create(h)

    resp = await client.get(f"/v1/harnesses/{h.id}")
    assert resp.status_code == 200
    body = resp.json()
    payload = json.loads(body["last_operation_error"])
    assert payload["code"] == "dependency_fetch_failed"
    assert payload["inner_code"] == "ref_not_found"


@pytest.mark.asyncio
async def test_get_returns_dependency_yaml_invalid_in_last_operation_error(
    client, fake_storage_provider,
):
    storage = fake_storage_provider.get_storage(Harness)
    err = {
        "code": "dependency_yaml_invalid",
        "message": "sub harness.yaml must declare metadata.name or metadata.slug",
        "git_url": "https://github.com/example/sub",
        "ref": "main",
    }
    h = _make_error_harness(
        "hns_erryaml01", last_operation_error=json.dumps(err),
    )
    await storage.create(h)

    resp = await client.get(f"/v1/harnesses/{h.id}")
    assert resp.status_code == 200
    body = resp.json()
    payload = json.loads(body["last_operation_error"])
    assert payload["code"] == "dependency_yaml_invalid"


@pytest.mark.asyncio
async def test_get_returns_apply_id_conflict_in_last_operation_error(
    client, fake_storage_provider,
):
    storage = fake_storage_provider.get_storage(Harness)
    err = {
        "code": "apply_id_conflict",
        "message": (
            "resolved id 'acme__widget' already belongs to harness 'h-other'"
        ),
        "conflicting_id": "acme__widget",
        "existing_harness_id": "h-other",
    }
    h = _make_error_harness(
        "hns_errconfl01", last_operation_error=json.dumps(err),
    )
    await storage.create(h)

    resp = await client.get(f"/v1/harnesses/{h.id}")
    assert resp.status_code == 200
    body = resp.json()
    payload = json.loads(body["last_operation_error"])
    assert payload["code"] == "apply_id_conflict"
    assert payload["conflicting_id"] == "acme__widget"
    assert payload["existing_harness_id"] == "h-other"
