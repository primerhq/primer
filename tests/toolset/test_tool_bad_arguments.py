"""Missing, ill-typed and null arguments to an internal tool are answered as a tool error, never raised past the handler (AG-M14).

The harness tools read ``arguments["id"]`` and ``int(arguments["offset"])`` and the python-toolset tools built their args model with ``_Args(**args)``
outside any ``try``: ``{}`` raised a bare ``KeyError`` or a pydantic ``ValidationError`` out of the provider. The loop answers only a ``PrimerError``
as a tool error, so the turn failed (on the claims path it was retried as transient up to the attempt cap) instead of the model fixing its call, and
the pydantic text quoted the submitted python source. They now validate with an args model and answer ``type=validation-error``, as every other
toolset does. The fuzz below is the review probe (``/tmp/review-agent/toolset/test_fuzz_args.py``) inverted.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.api.registries import ProviderRegistry
from primer.model.chat import ToolCallPart
from primer.model.except_ import PrimerError
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.yield_ import YieldToWorker
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.canvas import build_canvas_toolset
from primer.toolset.collections import build_collections_toolset
from primer.toolset.crud import build_crud_toolset
from primer.toolset.harness import build_harness_toolset_provider
from primer.toolset.misc import build_misc_toolset
from primer.toolset.system import build_system_toolset
from primer.toolset.trigger import build_trigger_toolset_provider
from primer.toolset.workspace_ext import build_workspace_ext_toolset
from primer.toolset.workspaces import build_workspaces_toolset
from tests._support.caller import ADMIN_CALLER

ADMIN = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")
SOURCE = "SECRET_SOURCE_MARKER = 'sk-python-source-0123456789'"


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "f.sqlite"))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    yield provider
    await provider.aclose()


def _registry(sp) -> ProviderRegistry:
    return ProviderRegistry(
        sp, llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda t: object(),
    )


def _bad(schema: dict) -> object:
    kind = schema.get("type")
    if kind == "string":
        return 12345
    if kind in ("integer", "number"):
        return "nope"
    if kind == "boolean":
        return "maybe"
    if kind in ("array", "object"):
        return "x"
    return {"zz": [1, "2"]}


def _variants(tool) -> list[tuple[str, dict]]:
    props = (tool.args_schema or {}).get("properties") or {}
    out = [("empty", {}), ("all-bad", {k: _bad(v) for k, v in props.items()}), ("all-none", dict.fromkeys(props))]
    for example in tool.examples or []:
        out.append(("example", dict(example.args)))
        out.extend((f"example-bad-{k}", {**example.args, k: _bad(props.get(k, {}))}) for k in example.args)
    return out


@pytest.mark.timeout(120)
async def test_no_internal_tool_raises_anything_but_a_primer_error_or_a_yield_on_bad_arguments(sp) -> None:
    registry = _registry(sp)
    toolsets = {
        "system": build_system_toolset(storage_provider=sp, provider_registry=registry),
        "crud": build_crud_toolset(storage_provider=sp),
        "trigger": build_trigger_toolset_provider(storage_provider=sp),
        "harness": build_harness_toolset_provider(storage_provider=sp),
        "misc": build_misc_toolset(),
        "collections": build_collections_toolset(storage_provider=sp),
        "workspace_ext": build_workspace_ext_toolset(storage_provider=sp),
        "canvas": build_canvas_toolset(storage_provider=sp),
        "workspaces": build_workspaces_toolset(storage_provider=sp, workspace_registry=AsyncMock()),
    }
    escapes: dict[tuple, str] = {}
    for ts_id, prov in toolsets.items():
        for tool in [t async for t in prov.list_tools()]:
            if tool.id in {"sleep", "watch_files"}:
                continue
            for label, args in _variants(tool):
                try:
                    await prov.call(tool_name=tool.id, arguments=args, ctx=ADMIN_CALLER)
                except (PrimerError, YieldToWorker):
                    pass
                except Exception as exc:  # noqa: BLE001 - what this test looks for
                    frames = traceback.extract_tb(exc.__traceback__)
                    if any("mock" in f.filename for f in frames) or "Mock" in str(exc):
                        continue  # the AsyncMock workspace registry, not the handler
                    escapes.setdefault((ts_id, tool.id, type(exc).__name__), f"{label} {str(args)[:120]}: {str(exc)[:160]}")
    assert escapes == {}


@pytest.mark.parametrize(
    ("call", "expected_type"),
    [
        (ToolCallPart(id="c1", name="system__call_tool", arguments={"toolset_id": "harness", "tool_name": "get", "arguments": {}}), "validation-error"),
        (ToolCallPart(id="c2", name="crud__list_python_tools", arguments={"id": "ts-1"}), "validation-error"),
    ],
)
async def test_a_bad_call_through_the_tool_manager_is_answered_as_a_validation_error(sp, call, expected_type) -> None:
    registry = _registry(sp)
    system = build_system_toolset(storage_provider=sp, provider_registry=registry)
    harness = build_harness_toolset_provider(storage_provider=sp)
    crud = build_crud_toolset(storage_provider=sp)
    registry._system_toolset_provider = system
    registry._harness_toolset_provider = harness
    registry._crud_toolset_provider = crud

    class _Session:
        session_id, workspace_id, agent_id = "sess-1", "ws-1", "operator"
        workspace_tools: list = []

    mgr = ToolExecutionManager.for_workspace(
        toolset_providers={"system": system, "crud": crud}, session=_Session(), provider_registry=registry,
        tools=["system__call_tool", "crud__list_python_tools"], initiated_by=ADMIN,
    )

    result = await mgr.execute(call)

    assert result.error is True
    assert expected_type in result.output, result.output


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("create_python_toolset", {"source": SOURCE}),
        ("create_python_toolset", {"toolset_id": ["x"], "source": SOURCE}),
        ("update_python_toolset_source", {"source": SOURCE}),
        ("list_python_tools", {"toolset_id": None}),
    ],
)
async def test_a_python_toolset_tool_answers_bad_arguments_without_the_submitted_source(sp, tool, arguments) -> None:
    crud = build_crud_toolset(storage_provider=sp)

    result = await crud.call(tool_name=tool, arguments=arguments, ctx=ADMIN_CALLER)

    assert result.is_error is True
    answer = json.loads(result.output)
    assert answer["type"] == "validation-error"
    assert answer["message"].startswith("argument validation failed: ")
    assert "sk-python-source" not in result.output and "SECRET_SOURCE_MARKER" not in result.output


@pytest.mark.parametrize(
    ("tool", "arguments", "field"),
    [
        ("get", {}, "id"),
        ("update", {"name": "n"}, "id"),
        ("update_overrides", {"overrides": {}}, "id"),
        ("fetch", {}, "id"),
        ("install", {"overrides": {}}, "id"),
        ("sync", {"overrides": {}}, "id"),
        ("uninstall", {}, "id"),
        ("register", {"slug": "abc", "git_url": "https://example.com/r"}, "name"),
        ("register", {"name": "n", "slug": 12345, "git_url": "https://example.com/r"}, "slug"),
        ("register", {"name": "n", "slug": "a", "git_url": "https://example.com/r"}, "slug"),
        ("list", {"offset": "nope"}, "offset"),
        ("list", {"length": None}, "length"),
        ("list", {"offset": -1}, "offset"),
    ],
)
async def test_a_harness_tool_answers_bad_arguments_as_a_validation_error_naming_the_field(sp, tool, arguments, field) -> None:
    harness = build_harness_toolset_provider(storage_provider=sp)

    result = await harness.call(tool_name=tool, arguments=arguments, ctx=ADMIN_CALLER)

    assert result.is_error is True
    answer = json.loads(result.output)
    assert answer["type"] == "validation-error", answer
    assert field in answer["message"], answer


# ---- characterization: what was already answered is answered the same --------------------------------------------------------------------


async def test_characterization_harness_answers_and_successes_are_unchanged(sp) -> None:
    harness = build_harness_toolset_provider(storage_provider=sp)

    missing = await harness.call(tool_name="get", arguments={"id": "hns-nope"}, ctx=ADMIN_CALLER)
    assert missing.is_error is True
    assert json.loads(missing.output) == {"type": "not-found", "message": "Harness 'hns-nope' does not exist"}

    bad_url = await harness.call(tool_name="register", arguments={"name": "n", "slug": "my-h", "git_url": "http://example.com/r"}, ctx=ADMIN_CALLER)
    assert bad_url.is_error is True and json.loads(bad_url.output)["type"] == "validation-error"

    created = await harness.call(tool_name="register", arguments={"name": "n", "slug": "my-h", "git_url": "https://example.com/r", "ref": ""}, ctx=ADMIN_CALLER)
    assert created.is_error is False
    row = json.loads(created.output)
    assert (row["slug"], row["ref"], row["status"]) == ("my-h", "main", "draft")

    # Ints sent as strings were accepted by int() and still are.
    listed = await harness.call(tool_name="list", arguments={"offset": "0", "length": "5", "slug": None}, ctx=ADMIN_CALLER)
    assert listed.is_error is False
    page = json.loads(listed.output)
    assert (page["total"], page["offset"], [i["slug"] for i in page["items"]]) == (1, 0, ["my-h"])

    for tool in ("update", "update_overrides", "fetch", "install", "sync", "uninstall"):
        answer = await harness.call(tool_name=tool, arguments={"id": "hns-nope", "overrides": {}}, ctx=ADMIN_CALLER)
        assert json.loads(answer.output) == {"type": "not-found", "message": "Harness 'hns-nope' does not exist"}, tool

    no_overrides = await harness.call(tool_name="install", arguments={"id": row["id"]}, ctx=ADMIN_CALLER)
    assert json.loads(no_overrides.output)["type"] == "overrides-schema-missing"


async def test_characterization_python_toolset_answers_are_unchanged(sp) -> None:
    crud = build_crud_toolset(storage_provider=sp)

    for tool, arguments in (
        ("list_python_tools", {"toolset_id": "nope"}),
        ("update_python_toolset_source", {"toolset_id": "nope", "source": "x = 1"}),
    ):
        answer = await crud.call(tool_name=tool, arguments=arguments, ctx=ADMIN_CALLER)
        assert (answer.is_error, json.loads(answer.output)) == (False, {"ok": False, "error": "not-found"}), tool
