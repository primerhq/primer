"""The python-toolset tools carry the Toolset guards the REST router carries (task 01a111d1, D5 phase 2a).

``create_python_toolset`` and ``update_python_toolset_source`` (primer/toolset/_python_tools.py, registered on the ``crud`` toolset,
admin-gated) write Toolset rows directly and bypassed every guard of the toolset router:

* a reserved scope id (``external`` / ``workspace`` / ``workspace_ext``, claimed by internal toolsets) could be created (REST 409);
* the source of a harness-managed toolset could be edited (REST 409).

The new refusals are TYPED errors (``conflict``, ``is_error`` True), like the other tools'. Registration failures and the unknown-id
answer keep their existing untyped ``{"ok": false, ...}`` shape (the lead's ruling): the controls below pin that.
"""

from __future__ import annotations

import json

import pytest

from primer.model.provider import Toolset, ToolsetProviderType
from primer.model.providers.toolset import PythonConfig
from primer.toolset.crud import build_crud_toolset

SRC_V1 = '''
@primer_tool()
def greet(name: str) -> str:
    """Greet a person by name.

    Use when you need a friendly greeting.

    Args:
        name: Who to greet.
    """
    return f"hello {name}"
'''
SRC_V2 = SRC_V1.replace("def greet(", "def salute(")
NO_DOCSTRING = "@primer_tool()\ndef nodoc(x: str) -> str:\n    return x\n"


async def _call(toolset, name: str, **args):
    result = await toolset.call(tool_name=name, arguments=args)
    try:
        body = json.loads(result.output)
    except ValueError:
        body = result.output
    return result.is_error, body


def _row(toolset_id: str, source: str = SRC_V1, **fields) -> Toolset:
    return Toolset(
        id=toolset_id, provider=ToolsetProviderType.PYTHON,
        config=PythonConfig(source=source, source_version=1, default_timeout_seconds=30), **fields,
    )


class TestCreatePythonToolset:
    @pytest.mark.parametrize("scope", ["external", "workspace", "workspace_ext"])
    async def test_a_reserved_scope_id_is_refused(self, fake_storage_provider, scope) -> None:
        toolset = build_crud_toolset(storage_provider=fake_storage_provider)

        is_error, answer = await _call(toolset, "create_python_toolset", toolset_id=scope, source=SRC_V1)

        assert is_error and answer["type"] == "conflict" and scope in answer["message"]
        assert await fake_storage_provider.get_storage(Toolset).get(scope) is None, "a python toolset took a reserved scope id"

    async def test_an_ordinary_id_is_created_as_before(self, fake_storage_provider) -> None:
        toolset = build_crud_toolset(storage_provider=fake_storage_provider)

        is_error, answer = await _call(toolset, "create_python_toolset", toolset_id="py-ok", source=SRC_V1)

        assert not is_error and answer["ok"] is True and [t["id"] for t in answer["tools"]] == ["greet"]
        assert await fake_storage_provider.get_storage(Toolset).get("py-ok") is not None

    async def test_a_registration_failure_keeps_its_untyped_answer(self, fake_storage_provider) -> None:
        toolset = build_crud_toolset(storage_provider=fake_storage_provider)

        is_error, answer = await _call(toolset, "create_python_toolset", toolset_id="py-bad", source=NO_DOCSTRING)

        assert not is_error and answer["ok"] is False and answer["error"]
        assert await fake_storage_provider.get_storage(Toolset).get("py-bad") is None


class TestUpdatePythonToolsetSource:
    async def test_a_managed_toolset_source_cannot_be_edited(self, fake_storage_provider) -> None:
        await fake_storage_provider.get_storage(Toolset).create(_row("py-managed", harness_id="hns_x"))
        toolset = build_crud_toolset(storage_provider=fake_storage_provider)

        is_error, answer = await _call(toolset, "update_python_toolset_source", toolset_id="py-managed", source=SRC_V2)

        assert is_error and answer["type"] == "conflict"
        stored = await fake_storage_provider.get_storage(Toolset).get("py-managed")
        assert stored.config.source == SRC_V1 and stored.config.source_version == 1, "a managed toolset's source was edited"

    async def test_an_unmanaged_toolset_is_updated_and_its_version_bumped_as_before(self, fake_storage_provider) -> None:
        await fake_storage_provider.get_storage(Toolset).create(_row("py-free"))
        toolset = build_crud_toolset(storage_provider=fake_storage_provider)

        is_error, answer = await _call(toolset, "update_python_toolset_source", toolset_id="py-free", source=SRC_V2)

        assert not is_error and answer["ok"] is True and answer["source_version"] == 2
        stored = await fake_storage_provider.get_storage(Toolset).get("py-free")
        assert stored.config.source == SRC_V2

    async def test_an_unknown_id_keeps_its_untyped_answer(self, fake_storage_provider) -> None:
        toolset = build_crud_toolset(storage_provider=fake_storage_provider)

        is_error, answer = await _call(toolset, "update_python_toolset_source", toolset_id="py-ghost", source=SRC_V2)

        assert not is_error and answer == {"ok": False, "error": "not-found"}
