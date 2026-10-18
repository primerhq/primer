"""``GET /v1/workspace_providers/_choices``: the provider list a signed-in user may see (platform revamp 2026-10, plan 08 task 7, design 14.2).

The full provider list is admin-only because every row carries its connection (a kubeconfig path, a service-account token, a remote runtime's
TLS key). A non-admin still has to name a provider in a template, so the choices route answers the least that needs: ``id``, ``kind``, ``label``
and ``default_image`` of every provider, sorted by id, and nothing else. It is user tier, and the literal path has to win over ``/{id}``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport

from primer.auth.passwords import hash_password
from primer.model.user import User
from primer.model.workspace import WorkspaceProvider
from primer.workspace import provider_choices
from tests.api.conftest import app, fake_provider_registry  # noqa: F401

_TOKEN = "sa-token-that-must-never-be-served"


@pytest.fixture
async def admin(app) -> AsyncIterator[httpx.AsyncClient]:  # noqa: F811
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # The first registration is the admin.
        r = await c.post("/v1/auth/register", json={"username": "choiceadmin", "password": "choiceadminpass1"})
        assert r.status_code == 200, r.text
        yield c


async def _signed_in(app, *, role: str, username: str) -> httpx.AsyncClient:  # noqa: F811
    """A client signed in as a fresh account with ``role``; the caller closes it."""
    await app.state.storage_provider.get_storage(User).create(
        User(
            id=f"user-{username}", username=username, password_hash=await hash_password(f"{username}pass1"),
            created_at=datetime.now(timezone.utc), role=role,
        )
    )
    c = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    r = await c.post("/v1/auth/login", json={"username": username, "password": f"{username}pass1"})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
async def user(app, admin) -> AsyncIterator[httpx.AsyncClient]:  # noqa: F811
    c = await _signed_in(app, role="user", username="choiceuser")
    try:
        yield c
    finally:
        await c.aclose()


@pytest.fixture
async def restricted(app, admin) -> AsyncIterator[httpx.AsyncClient]:  # noqa: F811
    c = await _signed_in(app, role="restricted", username="choicerestricted")
    try:
        yield c
    finally:
        await c.aclose()


def _container(provider_id: str, runtime: str = "docker") -> WorkspaceProvider:
    return WorkspaceProvider.model_validate({
        "id": provider_id, "provider": "container",
        "config": {
            "kind": "container", "runtime": runtime,
            "connection": {"kind": "socket", "socket_path": "/var/run/docker.sock"},
            "reachability": {"kind": "host_port", "bind_host": "127.0.0.1"},
        },
    })


def _kubernetes(provider_id: str, connection: dict | None = None) -> WorkspaceProvider:
    return WorkspaceProvider.model_validate({
        "id": provider_id, "provider": "kubernetes",
        "config": {
            "kind": "kubernetes", "variant": "system", "connection": connection or {"kind": "in_cluster"},
            "namespace": "primer-private-namespace", "reachability": {"kind": "in_cluster"},
        },
    })


def _local(provider_id: str) -> WorkspaceProvider:
    return WorkspaceProvider.model_validate(
        {"id": provider_id, "provider": "local", "config": {"kind": "local", "root_path": "/srv/private-root"}}
    )


async def _seed(app, *rows: WorkspaceProvider) -> None:  # noqa: F811
    for row in rows:
        await app.state.storage_provider.get_storage(WorkspaceProvider).create(row)


@pytest.mark.asyncio
async def test_a_user_lists_provider_choices(app, user) -> None:  # noqa: F811
    await _seed(app, _kubernetes("k3s"), _container("dock"))

    r = await user.get("/v1/workspace_providers/_choices")

    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert {"id": "dock", "kind": "container", "label": "Docker", "default_image": "primer/workspace-runtime:1.1"} in items
    assert {"id": "k3s", "kind": "kubernetes", "label": "Kubernetes", "default_image": None} in items
    assert all("config" not in item for item in items), items
    ids = [item["id"] for item in items]
    assert ids == sorted(ids), ids


@pytest.mark.asyncio
async def test_a_choice_is_four_fields_and_serves_nothing_of_the_connection(app, user) -> None:  # noqa: F811
    """The row of a Kubernetes provider holds a bearer token and a cluster address; none of it, and no other field of the row, is served."""
    await _seed(app, _kubernetes("k3s-token", {
        "kind": "service_account_token", "apiserver_url": "https://10.9.9.9:6443", "ca_data": "PEM-DATA", "token": _TOKEN,
    }), _local("local-private"))

    r = await user.get("/v1/workspace_providers/_choices")

    assert r.status_code == 200, r.text
    for secret in (_TOKEN, "10.9.9.9", "PEM-DATA", "primer-private-namespace", "/srv/private-root", "apiserver"):
        assert secret not in r.text, secret
    assert all(set(item) == {"id", "kind", "label", "default_image"} for item in r.json()["items"]), r.json()


@pytest.mark.asyncio
async def test_the_label_names_the_container_runtime_and_the_default_image_is_the_runtime_images(app, user) -> None:  # noqa: F811
    await _seed(app, _local("loc"), _container("dock"), _container("pod", "podman"), _container("ctd", "containerd"), _kubernetes("k3s"))

    r = await user.get("/v1/workspace_providers/_choices")

    by_id = {item["id"]: item for item in r.json()["items"]}
    assert (by_id["loc"]["kind"], by_id["loc"]["label"], by_id["loc"]["default_image"]) == ("local", "Local", None)
    assert (by_id["dock"]["kind"], by_id["dock"]["label"]) == ("container", "Docker")
    assert (by_id["pod"]["kind"], by_id["pod"]["label"]) == ("container", "Podman")
    assert (by_id["ctd"]["kind"], by_id["ctd"]["label"]) == ("container", "containerd")
    # Any container provider runs the runtime image unless a template names another; Local and Kubernetes have nothing to pre-fill.
    assert [by_id[i]["default_image"] for i in ("dock", "pod", "ctd")] == ["primer/workspace-runtime:1.1"] * 3
    assert by_id["k3s"]["default_image"] is None


@pytest.mark.asyncio
async def test_the_full_provider_list_stays_admin_only(app, user) -> None:  # noqa: F811
    await _seed(app, _container("dock"))

    r = await user.get("/v1/workspace_providers")

    assert r.status_code == 403, r.text
    assert r.json()["extensions"]["error"] == "forbidden_role"


@pytest.mark.asyncio
async def test_a_user_still_cannot_read_one_provider_by_id(app, user) -> None:  # noqa: F811
    await _seed(app, _container("dock"))

    assert (await user.get("/v1/workspace_providers/dock")).status_code == 403


@pytest.mark.asyncio
async def test_the_choices_path_is_not_read_as_a_provider_id(app, admin) -> None:  # noqa: F811
    r = await admin.get("/v1/workspace_providers/_choices")

    assert r.status_code == 200, r.text
    assert isinstance(r.json()["items"], list)


@pytest.mark.asyncio
async def test_an_admin_sees_the_same_choices_as_a_user(app, admin, user) -> None:  # noqa: F811
    await _seed(app, _container("dock"), _kubernetes("k3s"))

    assert (await admin.get("/v1/workspace_providers/_choices")).json() == (await user.get("/v1/workspace_providers/_choices")).json()


@pytest.mark.asyncio
async def test_a_restricted_user_gets_no_choices(app, restricted) -> None:  # noqa: F811
    """User tier means a signed-in user with a role that may use the feature routes, as the template and workspace routes."""
    assert (await restricted.get("/v1/workspace_providers/_choices")).status_code == 403


@pytest.mark.asyncio
async def test_a_signed_out_caller_gets_no_choices(app, admin) -> None:  # noqa: F811
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anonymous:
        assert (await anonymous.get("/v1/workspace_providers/_choices")).status_code == 401


@pytest.mark.asyncio
async def test_choices_page_through_every_provider(app, admin, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(provider_choices, "_PAGE", 2)
    await _seed(app, _container("p-1"), _container("p-2"), _container("p-3"))

    r = await admin.get("/v1/workspace_providers/_choices")

    assert {"p-1", "p-2", "p-3"} <= {item["id"] for item in r.json()["items"]}, r.json()


@pytest.mark.asyncio
async def test_more_than_one_full_page_of_providers_are_all_listed_once_and_sorted(app, admin, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(provider_choices, "_PAGE", 3)
    await _seed(app, *[_container(f"pg-{n:02d}") for n in (7, 3, 9, 1, 5, 2, 8)])

    ids = [item["id"] for item in (await admin.get("/v1/workspace_providers/_choices")).json()["items"]]

    paged = [i for i in ids if i.startswith("pg-")]
    assert paged == [f"pg-{n:02d}" for n in (1, 2, 3, 5, 7, 8, 9)], ids
    assert len(ids) == len(set(ids)), "a row on a page boundary was listed twice"


def test_every_provider_kind_has_a_choice() -> None:
    """A kind added to the model must get a label here, or listing the choices would answer 500 for everyone."""
    from typing import get_args

    from primer.model.workspace import WorkspaceProviderChoice, WorkspaceProviderType

    kinds = set(get_args(WorkspaceProviderChoice.model_fields["kind"].annotation))
    assert kinds == {t.value for t in WorkspaceProviderType}
