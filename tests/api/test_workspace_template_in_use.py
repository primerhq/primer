"""``DELETE /v1/workspace_templates/{id}`` is refused with a 409 ``template_in_use`` while a workspace was made from the template (lead ruling 6 of
plan 08, platform revamp 2026-10).

A workspace row names its template and the name is live: a Docker or Kubernetes workspace is re-attached by reading the template again, so a
template deleted under it broke the workspace for good, while the router said the workspace would "keep working without it" (e2e T0223 pinned
that for a Local provider only, which never reads the template again). The delete is refused now, for every provider kind, with the count and up
to five workspace ids in the answer, the way the provider router refuses a provider a template or a workspace uses. Pinned here on the real
router, with workspaces made through ``POST /v1/workspaces`` and seeded straight into the store.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport
from pydantic import SecretStr

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.auth.passwords import hash_password
from primer.model.user import User
from primer.model.workspace import (
    ContainerConnectionSocket,
    ContainerReachabilityHostPort,
    ContainerTemplateConfig,
    ContainerWorkspaceConfig,
    K8sConnectionInCluster,
    K8sReachabilityInCluster,
    KubernetesTemplateConfig,
    KubernetesWorkspaceConfig,
    LocalWorkspaceConfig,
    Workspace,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
)
from tests.api.test_workspaces import _FakeBackend, _SP

# provider kind -> (provider id, template id)
_KINDS = {"local": ("loc", "tpl-loc"), "container": ("dock", "tpl-dock"), "kubernetes": ("k3s", "tpl-k3s")}


@pytest.fixture
def sp() -> _SP:
    return _SP()


@pytest.fixture
def app(sp):
    registry = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda t: object(),
    )
    return create_test_app(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=registry,
        workspace_registry=WorkspaceRegistry(sp, factory=_FakeBackend),
    )


@pytest.fixture
async def admin(app, sp) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # The first registration is the admin.
        r = await c.post("/v1/auth/register", json={"username": "inuseadmin", "password": "inuseadminpass1"})
        assert r.status_code == 200, r.text
        providers, templates = sp.get_storage(WorkspaceProvider), sp.get_storage(WorkspaceTemplate)
        await providers.create(WorkspaceProvider(
            id="loc", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/tmp/primer-in-use-tests"),
        ))
        await providers.create(WorkspaceProvider(
            id="dock", provider=WorkspaceProviderType.CONTAINER,
            config=ContainerWorkspaceConfig(
                runtime="docker", connection=ContainerConnectionSocket(socket_path="/var/run/docker.sock"),
                reachability=ContainerReachabilityHostPort(),
            ),
        ))
        await providers.create(WorkspaceProvider(
            id="k3s", provider=WorkspaceProviderType.KUBERNETES,
            config=KubernetesWorkspaceConfig(connection=K8sConnectionInCluster(), namespace="ns", reachability=K8sReachabilityInCluster()),
        ))
        await templates.create(WorkspaceTemplate(id="tpl-loc", provider_id="loc", description="local"))
        await templates.create(WorkspaceTemplate(id="tpl-dock", provider_id="dock", description="docker", backend=ContainerTemplateConfig()))
        await templates.create(WorkspaceTemplate(
            id="tpl-k3s", provider_id="k3s", description="kubernetes", backend=KubernetesTemplateConfig(image="rt:1"),
        ))
        yield c


@pytest.fixture
async def user(app, admin) -> AsyncIterator[httpx.AsyncClient]:
    await app.state.storage_provider.get_storage(User).create(
        User(
            id="user-inuse", username="inuseuser", password_hash=await hash_password("inuseuserpass1"),
            created_at=datetime.now(timezone.utc), role="user",
        )
    )
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/auth/login", json={"username": "inuseuser", "password": "inuseuserpass1"})
        assert r.status_code == 200, r.text
        yield c


def _row(workspace_id: str, template_id: str, provider_id: str) -> Workspace:
    return Workspace(
        id=workspace_id, template_id=template_id, provider_id=provider_id, created_at=datetime.now(timezone.utc), phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959/", token=SecretStr("t")),
    )


async def _seed_workspaces(sp, template_id: str, provider_id: str, ids: list[str]) -> None:
    for workspace_id in ids:
        await sp.get_storage(Workspace).create(_row(workspace_id, template_id, provider_id))


@pytest.mark.parametrize("kind", sorted(_KINDS))
async def test_a_template_a_workspace_was_made_from_cannot_be_deleted(admin, user, sp, kind) -> None:
    provider_id, template_id = _KINDS[kind]
    made = await admin.post("/v1/workspaces", json={"id": "ws-one", "template_id": template_id})
    assert made.status_code == 201, made.text

    r = await user.delete(f"/v1/workspace_templates/{template_id}")

    assert r.status_code == 409, r.text
    body = r.json()
    assert body["status"] == 409 and body["type"].endswith("/conflict"), body
    ext = body["extensions"]
    assert ext["error"] == "template_in_use" and ext["kind"] == "workspace_template", ext
    assert ext["count"] == 1 and ext["workspace_ids"] == ["ws-one"], ext
    assert template_id in body["detail"] and "1 workspace" in body["detail"] and "ws-one" in body["detail"], body["detail"]
    assert ext["message"] == body["detail"]
    assert "/errors/internal" not in r.text
    assert await sp.get_storage(WorkspaceTemplate).get(template_id) is not None, "the refused delete took the template away"
    assert (await sp.get_storage(Workspace).get("ws-one")).provider_id == provider_id


async def test_an_admin_is_refused_the_same_way(admin, sp) -> None:
    await _seed_workspaces(sp, "tpl-loc", "loc", ["ws-a"])

    r = await admin.delete("/v1/workspace_templates/tpl-loc")

    assert r.status_code == 409 and r.json()["extensions"]["error"] == "template_in_use", r.text


async def test_the_refusal_names_the_count_and_up_to_five_ids(admin, sp) -> None:
    # Seeded in id order: the in-memory store of these tests answers in insertion order and ignores ``order_by``. That the first five are
    # the first five BY ID is pinned on a real SQLite store (tests/workspace/test_template_in_use.py and the tool tests in
    # tests/toolset/test_workspaces_guards.py); this test pins the route's answer: the count, the cap of five, and the extensions.
    await _seed_workspaces(sp, "tpl-dock", "dock", [f"ws-{n}" for n in range(1, 9)])

    r = await admin.delete("/v1/workspace_templates/tpl-dock")

    assert r.status_code == 409, r.text
    ext = r.json()["extensions"]
    assert ext["count"] == 8
    assert ext["workspace_ids"] == ["ws-1", "ws-2", "ws-3", "ws-4", "ws-5"], "the first five by id"
    detail = r.json()["detail"]
    assert "8 workspaces" in detail and "first 5: ws-1, ws-2, ws-3, ws-4, ws-5" in detail, detail
    assert "ws-6" not in detail and "ws-8" not in detail, "only five are named"


async def test_a_workspace_made_from_another_template_does_not_block(admin, sp) -> None:
    await _seed_workspaces(sp, "tpl-dock", "dock", ["ws-docker"])

    r = await admin.delete("/v1/workspace_templates/tpl-loc")

    assert r.status_code == 204, r.text
    assert await sp.get_storage(WorkspaceTemplate).get("tpl-loc") is None
    assert await sp.get_storage(WorkspaceTemplate).get("tpl-dock") is not None


async def test_a_template_nobody_used_is_deleted_as_before(admin, sp) -> None:
    r = await admin.delete("/v1/workspace_templates/tpl-k3s")

    assert r.status_code == 204, r.text
    assert (await admin.get("/v1/workspace_templates/tpl-k3s")).status_code == 404


@pytest.mark.parametrize("kind", sorted(_KINDS))
async def test_the_template_deletes_once_its_workspaces_are_deleted(admin, sp, kind) -> None:
    _, template_id = _KINDS[kind]
    for workspace_id in ("ws-one", "ws-two"):
        assert (await admin.post("/v1/workspaces", json={"id": workspace_id, "template_id": template_id})).status_code == 201

    refused = await admin.delete(f"/v1/workspace_templates/{template_id}")
    assert refused.status_code == 409 and refused.json()["extensions"]["count"] == 2, refused.text
    assert (await admin.delete("/v1/workspaces/ws-one")).status_code == 204
    still = await admin.delete(f"/v1/workspace_templates/{template_id}")
    assert still.status_code == 409 and still.json()["extensions"]["workspace_ids"] == ["ws-two"], still.text
    assert (await admin.delete("/v1/workspaces/ws-two")).status_code == 204

    assert (await admin.delete(f"/v1/workspace_templates/{template_id}")).status_code == 204
    assert (await admin.get(f"/v1/workspace_templates/{template_id}")).status_code == 404


async def test_editing_a_template_in_use_is_not_blocked(admin, sp) -> None:
    """Only the delete is guarded: an edit changes what the next create and re-attach read, and breaks nothing."""
    await _seed_workspaces(sp, "tpl-loc", "loc", ["ws-a"])
    body = (await admin.get("/v1/workspace_templates/tpl-loc")).json()
    body["description"] = "edited while in use"

    r = await admin.put("/v1/workspace_templates/tpl-loc", json=body)

    assert r.status_code == 200, r.text
    assert (await sp.get_storage(WorkspaceTemplate).get("tpl-loc")).description == "edited while in use"


_ADMIN_HELD = WorkspaceTemplate.model_validate({
    "id": "tpl-held", "provider_id": "loc", "description": "holds a secret file",
    "files": [{"path": "creds", "source": {"kind": "secret", "name": "OPENAI_API_KEY"}}],
})


async def test_a_caller_who_may_not_delete_the_template_is_told_so_before_the_workspaces(admin, user, sp) -> None:
    """403 first: a caller below admin never learns which workspaces use a template they may not touch."""
    await sp.get_storage(WorkspaceTemplate).create(_ADMIN_HELD)
    await _seed_workspaces(sp, "tpl-held", "loc", ["ws-secret-user"])

    forbidden = await user.delete("/v1/workspace_templates/tpl-held")
    refused = await admin.delete("/v1/workspace_templates/tpl-held")

    assert forbidden.status_code == 403 and forbidden.json()["extensions"]["error"] == "forbidden_role", forbidden.text
    assert "ws-secret-user" not in forbidden.text
    assert refused.status_code == 409 and refused.json()["extensions"]["workspace_ids"] == ["ws-secret-user"], refused.text


async def test_the_reserved_template_is_still_protected_first(admin, sp) -> None:
    from primer.bootstrap.defaults import RESERVED_WORKSPACE_TEMPLATES

    reserved = next(iter(RESERVED_WORKSPACE_TEMPLATES))
    await sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(id=reserved, provider_id="loc", description="reserved"))
    await _seed_workspaces(sp, reserved, "loc", ["ws-default"])

    r = await admin.delete(f"/v1/workspace_templates/{reserved}")

    assert r.status_code == 403 and r.json()["extensions"]["error"] == "reserved_id_protected", r.text


async def test_the_provider_guard_is_untouched(admin, sp) -> None:
    """The provider router already refused a provider a template uses: the new guard sits next to it, not in place of it."""
    r = await admin.delete("/v1/workspace_providers/loc")

    assert r.status_code == 409 and "in_use_by" in r.json()["detail"] and "tpl-loc" in r.json()["detail"], r.text


def test_the_openapi_documents_the_409_of_the_template_delete(app) -> None:
    responses = app.openapi()["paths"]["/v1/workspace_templates/{entity_id}"]["delete"]["responses"]

    assert {"403", "404", "409"} <= set(responses), sorted(responses)
