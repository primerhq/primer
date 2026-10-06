"""The ``crud`` toolset's agent and graph tools carry the managed-row guard the REST routers and the system toolset carry.

``build_crud_toolset`` re-homes ``create_/update_`` for agents and graphs from the generic factory
(:func:`primer.toolset._system_crud._crud_tools_for`) under the builder's scope. The factory's default is NO guards, so a caller has
to pass them: the system toolset does (task 01a111d1, D5 phase 1), and this toolset has to as well, or a builder can create a row
that claims a harness, or edit and release a harness-managed agent or graph (REST: 422 on create, 409 on update). Only create and
update are exposed here (no delete), so those are the two verbs pinned.

The last test is structural: every call of the factory under ``primer/toolset`` must pass ``guards=``, so a future caller cannot
silently get the empty default.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import primer.toolset
from primer.model.agent import Agent
from primer.model.graph import Graph
from primer.toolset.crud import build_crud_toolset

KINDS = {
    "agent": (Agent, {"id": "agent-m", "description": "managed", "model": {"profile_id": "prov--m"}}),
    "graph": (Graph, {"id": "graph-m", "description": "managed"}),
}


async def _call(toolset, name: str, **args):
    result = await toolset.call(tool_name=name, arguments=args)
    try:
        body = json.loads(result.output)
    except ValueError:
        body = result.output
    return result.is_error, body


@pytest.mark.parametrize("kind", sorted(KINDS))
class TestTheBuilderToolsetRefusesManagedRows:
    async def test_a_body_that_sets_harness_id_is_refused_on_create(self, fake_storage_provider, kind) -> None:
        model, body = KINDS[kind]
        toolset = build_crud_toolset(storage_provider=fake_storage_provider)

        is_error, answer = await _call(toolset, f"create_{kind}", entity={**body, "harness_id": "hns_x"})

        assert is_error and answer["type"] == "bad-request"
        assert await fake_storage_provider.get_storage(model).get(body["id"]) is None, "a managed row was created"

    async def test_a_managed_row_cannot_be_updated(self, fake_storage_provider, kind) -> None:
        model, body = KINDS[kind]
        await fake_storage_provider.get_storage(model).create(model.model_validate({**body, "harness_id": "hns_x"}))
        toolset = build_crud_toolset(storage_provider=fake_storage_provider)

        is_error, answer = await _call(
            toolset, f"update_{kind}", id=body["id"], entity={**body, "description": "edited", "harness_id": "hns_x"},
        )

        assert is_error and answer["type"] == "conflict"
        assert (await fake_storage_provider.get_storage(model).get(body["id"])).description == "managed"

    async def test_a_managed_row_cannot_be_released_by_an_update_that_omits_harness_id(
        self, fake_storage_provider, kind,
    ) -> None:
        model, body = KINDS[kind]
        await fake_storage_provider.get_storage(model).create(model.model_validate({**body, "harness_id": "hns_x"}))
        toolset = build_crud_toolset(storage_provider=fake_storage_provider)

        is_error, answer = await _call(toolset, f"update_{kind}", id=body["id"], entity=body)

        assert is_error and answer["type"] == "conflict"
        assert (await fake_storage_provider.get_storage(model).get(body["id"])).harness_id == "hns_x"

    async def test_an_unmanaged_row_is_created_and_updated_as_before(self, fake_storage_provider, kind) -> None:
        model, body = KINDS[kind]
        toolset = build_crud_toolset(storage_provider=fake_storage_provider)

        created_error, _ = await _call(toolset, f"create_{kind}", entity=body)
        updated_error, _ = await _call(toolset, f"update_{kind}", id=body["id"], entity={**body, "description": "edited"})

        assert not (created_error or updated_error)
        assert (await fake_storage_provider.get_storage(model).get(body["id"])).description == "edited"


def test_every_caller_of_the_generic_crud_factory_passes_guards() -> None:
    root = Path(primer.toolset.__file__).parent
    callers: list[str] = []
    missing: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if getattr(func, "id", getattr(func, "attr", None)) != "_crud_tools_for":
                continue
            callers.append(f"{path.name}:{node.lineno}")
            if not any(keyword.arg == "guards" for keyword in node.keywords):
                missing.append(f"{path.name}:{node.lineno}")

    assert len(callers) >= 2, f"the scan found no callers of _crud_tools_for ({callers}); the test would pass vacuously"
    assert not missing, f"callers of _crud_tools_for that pass no guards= (the factory default is none): {missing}"
