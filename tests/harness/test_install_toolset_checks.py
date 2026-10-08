"""apply_install / apply_sync run the shared toolset checks on every Toolset they write (AUTHZ-03 / FS-01).

The harness routes and tools are admin-only, so this is belt and braces: the install records who asked for it
(``Harness.operation_requested_by``), and a bundle that would create or change a stdio MCP toolset is refused as a whole unless
that requester may (``toolset_needs_admin``, ``primer.authz._role_allows``). A reserved toolset id is refused the same way. Nothing
is written when a check fails.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.harness.service import apply_install, apply_sync
from primer.model.agent import Agent
from primer.model.harness import Harness, HarnessRendering, RenderedEntry
from primer.model.principal import PrincipalRef
from primer.model.provider import Toolset

pytestmark = pytest.mark.asyncio

_ADMIN = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")
_USER = PrincipalRef(type="user", id="u-user", display="user", role="user", source="local")

_STDIO = {"provider": "mcp", "config": {"transport": "stdio", "config": {"command": ["sh", "-c", "id"]}}}
_HTTP = {"provider": "mcp", "config": {"transport": "http", "config": {"url": "https://mcp.example/mcp"}}}


def _harness(requested_by: PrincipalRef | None) -> Harness:
    return Harness(
        id="acme-id", slug="acme", name="Acme", git_url="https://github.com/x/y",
        operation_requested_by=requested_by, created_at=datetime.now(timezone.utc),
    )


def _toolset_entry(payload: dict, resolved_id: str = "acme__ts", rendered_hash: str = "r1") -> RenderedEntry:
    return RenderedEntry(
        kind="toolset", template_name="ts", resolved_id=resolved_id,
        template_source_hash="h", rendered_hash=rendered_hash, rendered_payload=payload,
    )


def _agent_entry() -> RenderedEntry:
    return RenderedEntry(
        kind="agent", template_name="asst", resolved_id="acme__asst",
        template_source_hash="h", rendered_hash="a1",
        rendered_payload={"description": "assistant", "model": {"profile_id": "p--m"}},
    )


async def _install(sp, harness, entries):
    return await apply_install(
        storage_provider=sp, harness=harness, entries=entries, rendered_files_by_name={},
        bundle_hash="bh", overrides_hash="oh", schema_hash=None,
    )


@pytest.mark.parametrize("requested_by", [_USER, None], ids=["user", "unknown"])
async def test_install_of_a_stdio_toolset_is_refused_unless_an_admin_asked(fake_storage_provider, requested_by):
    error = await _install(fake_storage_provider, _harness(requested_by), [_toolset_entry(_STDIO), _agent_entry()])

    assert error is not None
    assert json.loads(error)["code"] == "toolset_needs_admin"
    assert await fake_storage_provider.get_storage(Toolset).get("acme__ts") is None
    assert await fake_storage_provider.get_storage(Agent).get("acme__asst") is None, "the whole install must be refused"
    assert await fake_storage_provider.get_storage(HarnessRendering).get("acme-id") is None


async def test_install_of_a_stdio_toolset_requested_by_an_admin_proceeds(fake_storage_provider):
    error = await _install(fake_storage_provider, _harness(_ADMIN), [_toolset_entry(_STDIO)])

    assert error is None
    assert await fake_storage_provider.get_storage(Toolset).get("acme__ts") is not None


async def test_install_of_an_http_toolset_needs_no_admin(fake_storage_provider):
    error = await _install(fake_storage_provider, _harness(_USER), [_toolset_entry(_HTTP)])
    assert error is None


async def test_install_of_a_reserved_toolset_id_is_refused(fake_storage_provider):
    error = await _install(fake_storage_provider, _harness(_ADMIN), [_toolset_entry(_HTTP, resolved_id="external")])

    assert error is not None
    assert json.loads(error)["code"] == "reserved_id"
    assert await fake_storage_provider.get_storage(Toolset).get("external") is None


async def _seed_rendering(sp, entries):
    await sp.get_storage(HarnessRendering).create(
        HarnessRendering(
            id="acme-id", harness_id="acme-id", bundle_hash="old", overrides_hash="oh", schema_hash=None,
            entries=entries, rendered_at=datetime.now(timezone.utc),
        ),
    )


async def test_sync_that_adds_a_stdio_toolset_is_refused_for_a_user(fake_storage_provider):
    agent = _agent_entry()
    await _seed_rendering(fake_storage_provider, [])

    error = await apply_sync(
        storage_provider=fake_storage_provider, harness=_harness(_USER),
        new_entries=[_toolset_entry(_STDIO), agent], rendered_files_by_name={},
        bundle_hash="new", overrides_hash="oh", schema_hash=None,
    )

    assert error is not None and json.loads(error)["code"] == "toolset_needs_admin"
    assert await fake_storage_provider.get_storage(Toolset).get("acme__ts") is None
    assert await fake_storage_provider.get_storage(Agent).get("acme__asst") is None


async def test_sync_that_turns_an_http_toolset_into_stdio_is_refused_for_a_user(fake_storage_provider):
    old = _toolset_entry(_HTTP, rendered_hash="r-old")
    await _seed_rendering(fake_storage_provider, [old])
    await fake_storage_provider.get_storage(Toolset).create(
        Toolset.model_validate({"id": "acme__ts", **_HTTP, "harness_id": "acme-id"}),
    )

    error = await apply_sync(
        storage_provider=fake_storage_provider, harness=_harness(_USER),
        new_entries=[_toolset_entry(_STDIO, rendered_hash="r-new")], rendered_files_by_name={},
        bundle_hash="new", overrides_hash="oh", schema_hash=None,
    )

    assert error is not None and json.loads(error)["code"] == "toolset_needs_admin"
    stored = await fake_storage_provider.get_storage(Toolset).get("acme__ts")
    assert stored.config.transport.value == "http"


async def test_sync_that_adds_a_stdio_toolset_proceeds_for_an_admin(fake_storage_provider):
    await _seed_rendering(fake_storage_provider, [])

    error = await apply_sync(
        storage_provider=fake_storage_provider, harness=_harness(_ADMIN),
        new_entries=[_toolset_entry(_STDIO)], rendered_files_by_name={},
        bundle_hash="new", overrides_hash="oh", schema_hash=None,
    )

    assert error is None
    assert await fake_storage_provider.get_storage(Toolset).get("acme__ts") is not None


# ---- python toolsets and repoints go through the same rule (toolset_admin_reason, #476) ---------------------------------

_PYTHON = {
    "provider": "python",
    "config": {"source": "def hello() -> str:\n    \"\"\"Say hello.\"\"\"\n    return 'hi'\n", "source_version": 1},
}


async def _sync(sp, harness, entries):
    return await apply_sync(
        storage_provider=sp, harness=harness, new_entries=entries, rendered_files_by_name={},
        bundle_hash="new", overrides_hash="oh", schema_hash=None,
    )


@pytest.mark.parametrize("requested_by", [_USER, None], ids=["user", "unknown"])
async def test_install_of_a_python_toolset_is_refused_unless_an_admin_asked(fake_storage_provider, requested_by):
    error = await _install(fake_storage_provider, _harness(requested_by), [_toolset_entry(_PYTHON), _agent_entry()])

    assert error is not None and json.loads(error)["code"] == "toolset_needs_admin"
    assert "python toolset" in json.loads(error)["message"]
    assert await fake_storage_provider.get_storage(Toolset).get("acme__ts") is None
    assert await fake_storage_provider.get_storage(Agent).get("acme__asst") is None, "the whole install must be refused"


async def test_sync_that_adds_a_python_toolset_is_refused_for_a_user(fake_storage_provider):
    await _seed_rendering(fake_storage_provider, [])

    error = await _sync(fake_storage_provider, _harness(_USER), [_toolset_entry(_PYTHON), _agent_entry()])

    assert error is not None and json.loads(error)["code"] == "toolset_needs_admin"
    assert await fake_storage_provider.get_storage(Toolset).get("acme__ts") is None
    assert await fake_storage_provider.get_storage(Agent).get("acme__asst") is None


async def test_install_of_a_python_toolset_requested_by_an_admin_proceeds(fake_storage_provider):
    error = await _install(fake_storage_provider, _harness(_ADMIN), [_toolset_entry(_PYTHON)])

    assert error is None
    stored = await fake_storage_provider.get_storage(Toolset).get("acme__ts")
    assert stored is not None and stored.provider.value == "python"


_HTTP_WITH_SECRET = {
    "provider": "mcp",
    "config": {"transport": "http", "config": {"url": "https://mcp.example/mcp", "headers": {"Authorization": "Bearer stored"}}},
}


def _http(url: str, header: str) -> dict:
    return {"provider": "mcp", "config": {"transport": "http", "config": {"url": url, "headers": {"Authorization": header}}}}


async def _installed_http_toolset(sp) -> None:
    await _seed_rendering(sp, [_toolset_entry(_HTTP_WITH_SECRET, rendered_hash="r-old")])
    await sp.get_storage(Toolset).create(
        Toolset.model_validate({"id": "acme__ts", **_HTTP_WITH_SECRET, "harness_id": "acme-id"}),
    )


async def test_sync_that_repoints_a_toolset_keeping_its_masked_secret_is_refused_for_a_user(fake_storage_provider):
    await _installed_http_toolset(fake_storage_provider)

    error = await _sync(
        fake_storage_provider, _harness(_USER),
        [_toolset_entry(_http("https://elsewhere.example/mcp", "**********"), rendered_hash="r-new")],
    )

    assert error is not None and json.loads(error)["code"] == "toolset_needs_admin"
    stored = await fake_storage_provider.get_storage(Toolset).get("acme__ts")
    assert stored.config.config.url == "https://mcp.example/mcp"


async def test_sync_that_repoints_a_toolset_with_its_secret_re_entered_needs_no_admin(fake_storage_provider):
    await _installed_http_toolset(fake_storage_provider)

    error = await _sync(
        fake_storage_provider, _harness(_USER),
        [_toolset_entry(_http("https://elsewhere.example/mcp", "Bearer fresh"), rendered_hash="r-new")],
    )

    assert error is None
    stored = await fake_storage_provider.get_storage(Toolset).get("acme__ts")
    assert stored.config.config.url == "https://elsewhere.example/mcp"


async def test_sync_that_changes_a_stdio_toolsets_command_is_refused_for_a_user(fake_storage_provider):
    await _seed_rendering(fake_storage_provider, [_toolset_entry(_STDIO, rendered_hash="r-old")])
    await fake_storage_provider.get_storage(Toolset).create(
        Toolset.model_validate({"id": "acme__ts", **_STDIO, "harness_id": "acme-id"}),
    )
    other_command = {"provider": "mcp", "config": {"transport": "stdio", "config": {"command": ["sh", "-c", "whoami"]}}}

    error = await _sync(fake_storage_provider, _harness(_USER), [_toolset_entry(other_command, rendered_hash="r-new")])

    assert error is not None and json.loads(error)["code"] == "toolset_needs_admin"
    stored = await fake_storage_provider.get_storage(Toolset).get("acme__ts")
    assert stored.config.config.command == ["sh", "-c", "id"]
