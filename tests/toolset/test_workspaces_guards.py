"""The workspaces toolset's provider and template tools carry the REST routers' guards (task 01a111d1, D5 phase 1, item b).

The REST workspace routers refuse the reserved bootstrap rows (the ``local`` provider, the reserved templates) and refuse to delete a
provider a template or a workspace still references (a BDD pass found a deleted provider answering 204 and leaving every dependent
workspace unable to open a session). The toolset's create / update / delete handlers had neither, so an agent could delete the
reserved ``local`` provider or a provider in use. Pinned here, on a real sqlite store through the real toolset:

* a reserved id cannot be created (conflict), updated or deleted (forbidden; the delete holds even when no row exists);
* a provider referenced by a template or a workspace cannot be deleted (conflict);
* a template a workspace was made from can be deleted because the workspace
  keeps the create-time template snapshot used for re-attachment;
* a template that holds admin-only settings keeps its independent role guard.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import SecretStr

from primer.api.registries import WorkspaceRegistry
from primer.bootstrap.defaults import RESERVED_WORKSPACE_TEMPLATES
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.workspace import Workspace, WorkspaceProvider, WorkspaceRuntimeMeta, WorkspaceTemplate
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.workspaces import build_workspaces_toolset
from tests.toolset.test_workspaces import _provider, _StubBackend, _template_body

RESERVED_TEMPLATE_ID = next(iter(RESERVED_WORKSPACE_TEMPLATES))


@pytest.fixture
async def world(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    registry = WorkspaceRegistry(sp, factory=_StubBackend)
    toolset = build_workspaces_toolset(storage_provider=sp, workspace_registry=registry)
    yield sp, toolset
    await sp.aclose()


def _ctx(role: str) -> ToolContext:
    return ToolContext(
        tool_call_id="call-1", session_id="sess-1", workspace_id=None,
        initiated_by=PrincipalRef(type="user", id="user-1", display="user", role=role, source="local"),
    )


async def _call(toolset, name: str, *, ctx: ToolContext | None = None, **args):
    result = await toolset.call(tool_name=name, arguments=args, ctx=ctx)
    try:
        body = json.loads(result.output)
    except ValueError:
        body = result.output
    return result.is_error, body


def _provider_body(provider_id: str = "local-1") -> dict:
    return {**_provider().model_dump(mode="json"), "id": provider_id}


def _template(template_id: str = "tpl-1", provider_id: str = "local-1") -> WorkspaceTemplate:
    return WorkspaceTemplate.model_validate({**_template_body(), "id": template_id, "provider_id": provider_id})


def _workspace(workspace_id: str = "ws-1", provider_id: str = "local-1", template_id: str = "tpl-1") -> Workspace:
    template = _template(template_id, provider_id)
    return Workspace(
        id=workspace_id, template_id=template_id, template_snapshot=template, provider_id=provider_id,
        created_at=datetime.now(timezone.utc), phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959/", token=SecretStr("t")),
    )


class TestWorkspaceProviderGuards:
    @pytest.mark.asyncio
    async def test_the_reserved_local_provider_id_cannot_be_created(self, world) -> None:
        sp, toolset = world

        is_error, answer = await _call(toolset, "create_workspace_provider", entity=_provider_body("local"))

        assert is_error and answer["type"] == "conflict" and "reserved" in answer["message"]
        assert await sp.get_storage(WorkspaceProvider).get("local") is None

    @pytest.mark.asyncio
    async def test_the_reserved_local_provider_id_cannot_be_deleted_even_when_no_row_exists(self, world) -> None:
        _, toolset = world

        is_error, answer = await _call(toolset, "delete_workspace_provider", id="local")

        assert is_error and answer["type"] == "forbidden", "the delete went on to the row lookup"

    @pytest.mark.asyncio
    async def test_a_reserved_provider_row_that_exists_survives_a_delete(self, world) -> None:
        sp, toolset = world
        await sp.get_storage(WorkspaceProvider).create(WorkspaceProvider.model_validate(_provider_body("local")))

        is_error, answer = await _call(toolset, "delete_workspace_provider", id="local")

        assert is_error and answer["type"] == "forbidden"
        assert await sp.get_storage(WorkspaceProvider).get("local") is not None

    @pytest.mark.asyncio
    async def test_a_provider_a_template_uses_cannot_be_deleted(self, world) -> None:
        sp, toolset = world
        await sp.get_storage(WorkspaceProvider).create(WorkspaceProvider.model_validate(_provider_body()))
        await sp.get_storage(WorkspaceTemplate).create(_template())

        is_error, answer = await _call(toolset, "delete_workspace_provider", id="local-1")

        assert is_error and answer["type"] == "conflict"
        assert "in_use_by" in answer["message"] and "tpl-1" in answer["message"]
        assert await sp.get_storage(WorkspaceProvider).get("local-1") is not None

    @pytest.mark.asyncio
    async def test_a_provider_a_workspace_uses_cannot_be_deleted(self, world) -> None:
        sp, toolset = world
        await sp.get_storage(WorkspaceProvider).create(WorkspaceProvider.model_validate(_provider_body()))
        await sp.get_storage(Workspace).create(_workspace())

        is_error, answer = await _call(toolset, "delete_workspace_provider", id="local-1")

        assert is_error and answer["type"] == "conflict" and "ws-1" in answer["message"]
        assert await sp.get_storage(WorkspaceProvider).get("local-1") is not None

    @pytest.mark.asyncio
    async def test_an_unreferenced_provider_is_created_and_deleted_as_before(self, world) -> None:
        sp, toolset = world

        created_error, _ = await _call(toolset, "create_workspace_provider", entity=_provider_body())
        deleted_error, _ = await _call(toolset, "delete_workspace_provider", id="local-1")

        assert not (created_error or deleted_error)
        assert await sp.get_storage(WorkspaceProvider).get("local-1") is None


class TestWorkspaceTemplateGuards:
    @pytest.mark.asyncio
    async def test_a_reserved_template_id_cannot_be_created(self, world) -> None:
        sp, toolset = world

        is_error, answer = await _call(
            toolset, "create_workspace_template", entity={**_template_body(), "id": RESERVED_TEMPLATE_ID},
        )

        assert is_error and answer["type"] == "conflict" and "reserved" in answer["message"]
        assert await sp.get_storage(WorkspaceTemplate).get(RESERVED_TEMPLATE_ID) is None

    @pytest.mark.asyncio
    async def test_a_reserved_template_cannot_be_updated(self, world) -> None:
        sp, toolset = world
        await sp.get_storage(WorkspaceTemplate).create(_template(RESERVED_TEMPLATE_ID))
        _, served = await _call(toolset, "get_workspace_template", id=RESERVED_TEMPLATE_ID)
        served["description"] = "edited by an agent"

        is_error, answer = await _call(toolset, "update_workspace_template", id=RESERVED_TEMPLATE_ID, entity=served)

        assert is_error and answer["type"] == "forbidden"
        assert (await sp.get_storage(WorkspaceTemplate).get(RESERVED_TEMPLATE_ID)).description != "edited by an agent"

    @pytest.mark.asyncio
    async def test_a_reserved_template_id_cannot_be_deleted_even_when_no_row_exists(self, world) -> None:
        _, toolset = world

        is_error, answer = await _call(toolset, "delete_workspace_template", id=RESERVED_TEMPLATE_ID)

        assert is_error and answer["type"] == "forbidden", "the delete went on to the row lookup"

    @pytest.mark.asyncio
    async def test_an_ordinary_template_is_created_updated_and_deleted_as_before(self, world) -> None:
        sp, toolset = world

        created_error, _ = await _call(toolset, "create_workspace_template", entity=_template_body())
        _, served = await _call(toolset, "get_workspace_template", id="tpl-1")
        served["description"] = "renamed"
        updated_error, _ = await _call(toolset, "update_workspace_template", id="tpl-1", entity=served)
        deleted_error, _ = await _call(toolset, "delete_workspace_template", id="tpl-1")

        assert not (created_error or updated_error or deleted_error)
        assert await sp.get_storage(WorkspaceTemplate).get("tpl-1") is None

    @pytest.mark.asyncio
    async def test_a_template_a_workspace_was_made_from_can_be_deleted(self, world) -> None:
        sp, toolset = world
        await sp.get_storage(WorkspaceTemplate).create(_template())
        await sp.get_storage(Workspace).create(_workspace())

        is_error, answer = await _call(toolset, "delete_workspace_template", id="tpl-1")

        assert not is_error, answer
        assert await sp.get_storage(WorkspaceTemplate).get("tpl-1") is None
        row = await sp.get_storage(Workspace).get("ws-1")
        assert row is not None and row.template_snapshot is not None
        assert row.template_snapshot.id == "tpl-1"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["local", "container", "kubernetes"])
    async def test_every_provider_kind_keeps_a_snapshot_through_the_tools(self, world, kind) -> None:
        """The whole path through the tools, on each kind of provider: provider row, template and workspace made by the tools themselves."""
        sp, toolset = world
        providers = {
            "local": {"id": "p-local", "provider": "local", "config": {"kind": "local", "root_path": "/tmp/primer-in-use-tools"}},
            "container": {
                "id": "p-docker", "provider": "container",
                "config": {
                    "kind": "container", "runtime": "docker", "connection": {"kind": "socket", "socket_path": "/var/run/docker.sock"},
                    "reachability": {"kind": "host_port", "bind_host": "127.0.0.1"},
                },
            },
            "kubernetes": {
                "id": "p-k8s", "provider": "kubernetes",
                "config": {
                    "kind": "kubernetes", "variant": "system", "connection": {"kind": "in_cluster"}, "namespace": "ns",
                    "reachability": {"kind": "in_cluster"},
                },
            },
        }
        backends = {"local": {"kind": "local"}, "container": {"kind": "container"}, "kubernetes": {"kind": "kubernetes", "image": "rt:1"}}
        provider = providers[kind]
        await sp.get_storage(WorkspaceProvider).create(WorkspaceProvider.model_validate(provider))
        created, _ = await _call(toolset, "create_workspace_template", entity={
            "id": "tpl-kind", "description": kind, "provider_id": provider["id"], "backend": backends[kind],
        })
        made, made_row = await _call(toolset, "create_workspace", id="ws-made", template_id="tpl-kind")
        assert not (created or made), made_row

        delete_error, answer = await _call(
            toolset, "delete_workspace_template", id="tpl-kind",
        )
        assert not delete_error, answer
        assert await sp.get_storage(WorkspaceTemplate).get("tpl-kind") is None
        workspace = await sp.get_storage(Workspace).get("ws-made")
        assert workspace is not None and workspace.template_snapshot is not None
        assert workspace.template_snapshot.id == "tpl-kind"

    @pytest.mark.asyncio
    async def test_many_workspaces_do_not_block_template_deletion(self, world) -> None:
        sp, toolset = world
        await sp.get_storage(WorkspaceTemplate).create(_template())
        for n in (7, 3, 5, 1, 6, 2, 4):
            await sp.get_storage(Workspace).create(_workspace(f"ws-{n}"))

        is_error, answer = await _call(toolset, "delete_workspace_template", id="tpl-1")

        assert not is_error, answer
        assert await sp.get_storage(WorkspaceTemplate).get("tpl-1") is None

    @pytest.mark.asyncio
    async def test_a_workspace_made_from_another_template_does_not_block(self, world) -> None:
        sp, toolset = world
        await sp.get_storage(WorkspaceTemplate).create(_template())
        await sp.get_storage(Workspace).create(_workspace().model_copy(update={"template_id": "tpl-other"}))

        is_error, answer = await _call(toolset, "delete_workspace_template", id="tpl-1")

        assert not is_error, answer
        assert await sp.get_storage(WorkspaceTemplate).get("tpl-1") is None

    @pytest.mark.asyncio
    async def test_editing_a_template_a_workspace_uses_is_not_blocked(self, world) -> None:
        """Editing the source template does not change the stored snapshot."""
        sp, toolset = world
        await sp.get_storage(WorkspaceTemplate).create(_template())
        await sp.get_storage(Workspace).create(_workspace())
        _, served = await _call(toolset, "get_workspace_template", id="tpl-1")
        served["description"] = "edited while in use"

        is_error, answer = await _call(toolset, "update_workspace_template", id="tpl-1", entity=served)

        assert not is_error, answer
        assert (await sp.get_storage(WorkspaceTemplate).get("tpl-1")).description == "edited while in use"
        row = await sp.get_storage(Workspace).get("ws-1")
        assert row is not None and row.template_snapshot is not None
        assert row.template_snapshot.description == "dev"

    @pytest.mark.asyncio
    async def test_admin_only_settings_still_guard_template_deletion(self, world) -> None:
        sp, toolset = world
        held = WorkspaceTemplate.model_validate({
            **_template_body(), "id": "tpl-held", "files": [{"path": "creds", "source": {"kind": "secret", "name": "OPENAI_API_KEY"}}],
        })
        await sp.get_storage(WorkspaceTemplate).create(held)
        await sp.get_storage(Workspace).create(_workspace().model_copy(update={"template_id": "tpl-held", "id": "ws-secret-user"}))

        user_error, user_answer = await _call(toolset, "delete_workspace_template", ctx=_ctx("user"), id="tpl-held")
        admin_error, admin_answer = await _call(toolset, "delete_workspace_template", ctx=_ctx("admin"), id="tpl-held")

        assert user_error and user_answer["type"] == "forbidden" and "ws-secret-user" not in user_answer["message"], user_answer
        assert not admin_error, admin_answer
        assert await sp.get_storage(WorkspaceTemplate).get("tpl-held") is None

    @pytest.mark.asyncio
    async def test_create_workspace_rejects_an_id_that_fails_the_id_rule(self, world) -> None:
        """The id names a directory under the workspace root (#680 N7): a traversal id is a validation error and no row is
        written, on the tool path as on REST."""
        sp, toolset = world
        created, _ = await _call(toolset, "create_workspace_provider", entity=_provider_body())
        assert not created, "the local-1 provider must exist so the refusal is about the id, not the provider"
        await sp.get_storage(WorkspaceTemplate).create(_template())

        for wid in ("../escape", "/abs", ".", "..", "a/b", "a\x00b"):
            is_error, body = await _call(
                toolset, "create_workspace", id=wid, template_id="tpl-1"
            )

            assert is_error, (wid, body)
            assert body["type"] == "validation-error"

        assert await sp.get_storage(Workspace).get("../escape") is None
