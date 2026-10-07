"""Security review 2026-10-08 (INJ-01, AUTHZ-02, INJ-02, AUTHZ-05, SSRF-06): what a role=user caller may write.

The workspace template, workspace and terminal-access routes are on the user tier. Before this change a role=user caller
could:

* grant themselves the admin-only integrated terminal (``PUT /workspaces/{id}/terminal_access``);
* write a template whose container ``extra_mounts`` bind a host path, or whose Kubernetes ``extra_volumes``,
  ``extra_volume_mounts``, ``pod_overrides`` or ``container_security_context_overrides`` reach the node or the cluster;
* mount any ``PRIMER_SECRET_*`` secret into a workspace through a ``kind=secret`` file source, on a template or in the
  per-workspace overrides.

Each of those is admin-only now: a non-admin write that sets one of them, or changes it from the stored value, answers
403 ``forbidden_role``. The rest of a template stays user-tier, and an admin may still write all of it. A Kubernetes overlay
outside the allowlist is refused for an admin too (422).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport
from pydantic import SecretStr

from primer.auth.passwords import hash_password
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta, WorkspaceTemplate
from tests.api.conftest import app, fake_provider_registry  # noqa: F401


@pytest.fixture
async def admin(app) -> AsyncIterator[httpx.AsyncClient]:  # noqa: F811
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # The first registration is the admin.
        r = await c.post("/v1/auth/register", json={"username": "tpladmin", "password": "tpladminpass1"})
        assert r.status_code == 200, r.text
        yield c


@pytest.fixture
async def user(app, admin) -> AsyncIterator[httpx.AsyncClient]:  # noqa: F811
    await app.state.storage_provider.get_storage(User).create(
        User(
            id="user-tpl", username="tpluser", password_hash=await hash_password("tpluserpass1"),
            created_at=datetime.now(timezone.utc), role="user",
        )
    )
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/auth/login", json={"username": "tpluser", "password": "tpluserpass1"})
        assert r.status_code == 200, r.text
        yield c


def _container(**backend) -> dict:
    return {"id": "tpl-c", "provider_id": "p-1", "description": "d", "backend": {"kind": "container", "image": "alpine:3", **backend}}


def _k8s(**backend) -> dict:
    return {"id": "tpl-k", "provider_id": "p-1", "description": "d", "backend": {"kind": "kubernetes", "image": "alpine:3", **backend}}


_SECRET_FILE = {"path": "creds", "source": {"kind": "secret", "name": "OPENAI_API_KEY"}}

# One body per admin-only field, each with a value the k8s allowlist accepts (so the admin control is a 201).
_GATED_BODIES = {
    "extra_mounts": _container(extra_mounts=[{"host": "/", "container": "/host"}]),
    "extra_volumes": _k8s(extra_volumes=[{"name": "scratch", "emptyDir": {}}]),
    "extra_volume_mounts": _k8s(extra_volume_mounts=[{"name": "scratch", "mountPath": "/scratch"}]),
    "pod_overrides": _k8s(pod_overrides={"dnsPolicy": "ClusterFirst"}),
    "files.secret": {"id": "tpl-s", "provider_id": "p-1", "description": "d", "files": [_SECRET_FILE]},
}
# The k8s allowlist refuses every non-empty container_security_context_overrides (the backend never applied it), so an
# admin cannot set one either (see the allowlist test below); a user gets the 403 first.
_USER_GATED_BODIES = {
    **_GATED_BODIES,
    "container_security_context_overrides": _k8s(container_security_context_overrides={"readOnlyRootFilesystem": True}),
}


def _assert_forbidden_role(r: httpx.Response, field: str) -> None:
    assert r.status_code == 403, r.text
    ext = r.json()["extensions"]
    assert ext["error"] == "forbidden_role", r.text
    assert any(field in f for f in ext["fields"]), r.text


# ---------------------------------------------------------------------------
# INJ-01: the terminal grant is admin-only
# ---------------------------------------------------------------------------


async def _seed_workspace(app, wid: str = "ws-term") -> None:  # noqa: F811
    await app.state.storage_provider.get_storage(Workspace).create(Workspace(
        id=wid, template_id="tpl-1", provider_id="p-1", created_at=datetime.now(timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:1/", token=SecretStr("t")),
    ))


@pytest.mark.asyncio
async def test_a_user_cannot_grant_the_terminal(app, user) -> None:  # noqa: F811
    await _seed_workspace(app)

    r = await user.put("/v1/workspaces/ws-term/terminal_access", json={"enabled": True})

    assert r.status_code == 403, r.text
    assert r.json()["extensions"]["error"] == "forbidden_role"
    row = await app.state.storage_provider.get_storage(Workspace).get("ws-term")
    assert row.terminal_user_access is False


@pytest.mark.asyncio
async def test_a_user_cannot_revoke_the_terminal_either(app, admin, user) -> None:  # noqa: F811
    await _seed_workspace(app)
    assert (await admin.put("/v1/workspaces/ws-term/terminal_access", json={"enabled": True})).status_code == 200

    r = await user.put("/v1/workspaces/ws-term/terminal_access", json={"enabled": False})

    assert r.status_code == 403, r.text
    row = await app.state.storage_provider.get_storage(Workspace).get("ws-term")
    assert row.terminal_user_access is True


@pytest.mark.asyncio
async def test_an_admin_can_grant_the_terminal(app, admin) -> None:  # noqa: F811
    await _seed_workspace(app)

    r = await admin.put("/v1/workspaces/ws-term/terminal_access", json={"enabled": True})

    assert r.status_code == 200, r.text
    assert r.json()["terminal_user_access"] is True


# ---------------------------------------------------------------------------
# AUTHZ-02 / INJ-02 / INJ-05 / AUTHZ-05 / SSRF-06: admin-only template fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("field", sorted(_USER_GATED_BODIES))
async def test_a_user_cannot_create_a_template_that_sets_an_admin_only_field(app, user, field) -> None:  # noqa: F811
    body = _USER_GATED_BODIES[field]

    r = await user.post("/v1/workspace_templates", json=body)

    _assert_forbidden_role(r, field)
    assert await app.state.storage_provider.get_storage(WorkspaceTemplate).get(body["id"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", sorted(_GATED_BODIES))
async def test_an_admin_can_create_a_template_that_sets_an_admin_only_field(app, admin, field) -> None:  # noqa: F811
    r = await admin.post("/v1/workspace_templates", json=_GATED_BODIES[field])

    assert r.status_code == 201, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("field", sorted(_USER_GATED_BODIES))
async def test_a_user_cannot_add_an_admin_only_field_to_a_plain_template(app, admin, user, field) -> None:  # noqa: F811
    body = _USER_GATED_BODIES[field]
    plain = {**body, "files": [], "backend": {k: v for k, v in body.get("backend", {"kind": "local"}).items()
                                               if k in ("kind", "image")}}
    assert (await admin.post("/v1/workspace_templates", json=plain)).status_code == 201

    r = await user.put(f"/v1/workspace_templates/{body['id']}", json=body)

    _assert_forbidden_role(r, field)
    stored = await app.state.storage_provider.get_storage(WorkspaceTemplate).get(body["id"])
    assert stored.model_dump(mode="json")["backend"] == WorkspaceTemplate.model_validate(plain).model_dump(mode="json")["backend"]
    assert stored.files == []


@pytest.mark.asyncio
async def test_a_user_cannot_change_an_admin_only_field_an_admin_set(app, admin, user) -> None:  # noqa: F811
    assert (await admin.post("/v1/workspace_templates", json=_GATED_BODIES["pod_overrides"])).status_code == 201

    changed = _k8s(pod_overrides={"dnsPolicy": "Default"})
    r = await user.put("/v1/workspace_templates/tpl-k", json=changed)
    _assert_forbidden_role(r, "pod_overrides")

    cleared = _k8s()
    r = await user.put("/v1/workspace_templates/tpl-k", json=cleared)
    _assert_forbidden_role(r, "pod_overrides")


@pytest.mark.asyncio
async def test_a_user_may_edit_the_rest_of_a_template_that_keeps_an_admin_set_field(app, admin, user) -> None:  # noqa: F811
    """The gate is on the admin-only fields, not on the template: an unchanged privileged value passes."""
    assert (await admin.post("/v1/workspace_templates", json=_GATED_BODIES["extra_mounts"])).status_code == 201

    body = {**_GATED_BODIES["extra_mounts"], "description": "edited by a user", "init_commands": ["echo hi"]}
    r = await user.put("/v1/workspace_templates/tpl-c", json=body)

    assert r.status_code == 200, r.text
    assert r.json()["description"] == "edited by a user"


@pytest.mark.asyncio
async def test_a_user_may_still_write_a_template_without_admin_only_fields(app, user) -> None:  # noqa: F811
    body = {
        "id": "tpl-u", "provider_id": "p-1", "description": "d",
        "backend": {"kind": "kubernetes", "image": "alpine:3", "cpu_limit": "1"},
        "files": [{"path": "a.txt", "source": {"kind": "inline", "content": "hi"}}],
        "init_commands": ["echo hi"],
        "env": {"A": "1"},
    }

    r = await user.post("/v1/workspace_templates", json=body)

    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# The Kubernetes allowlist holds for an admin too (INJ-02)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [
    {"extra_volumes": [{"name": "s", "secret": {"secretName": "primer-db"}}]},
    {"extra_volumes": [{"name": "h", "hostPath": {"path": "/"}}]},
    {"pod_overrides": {"serviceAccountName": "primer-admin"}},
    {"pod_overrides": {"hostNetwork": True}},
    {"container_security_context_overrides": {"privileged": True}},
])
async def test_an_admin_template_outside_the_k8s_allowlist_is_refused(app, admin, backend) -> None:  # noqa: F811
    r = await admin.post("/v1/workspace_templates", json=_k8s(**backend))

    assert r.status_code == 422, r.text
    assert await app.state.storage_provider.get_storage(WorkspaceTemplate).get("tpl-k") is None


# ---------------------------------------------------------------------------
# AUTHZ-05 / SSRF-06: a secret file source in the per-workspace overrides
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_user_cannot_mount_a_secret_through_workspace_overrides(app, admin, user) -> None:  # noqa: F811
    assert (await admin.post("/v1/workspace_templates", json={"id": "tpl-p", "provider_id": "p-1", "description": "d"})).status_code == 201

    r = await user.post("/v1/workspaces", json={
        "template_id": "tpl-p", "overrides": {"files": [_SECRET_FILE]},
    })

    _assert_forbidden_role(r, "overrides.files")
    page = await app.state.storage_provider.get_storage(Workspace).list(OffsetPage(offset=0, length=10))
    assert [w.id for w in page.items] == []
