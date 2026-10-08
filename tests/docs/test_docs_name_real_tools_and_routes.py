"""The docs name only tools and endpoints that exist (finding A-19 of the 2026-10-08 review).

The agent-facing docs (``docs/agents/``) are ingested into ``_internal_ai_docs`` and served to agents by search, so a tool name in them is
acted on: ``docs/agents/workspaces.md`` told an agent to bind a workspace to a channel with ``system::set_workspace_channel_association`` and
``system::clear_workspace_channel_association``, neither of which has been a tool since the field became a reply binding
(``set_reply_binding`` / ``clear_reply_binding``), and a cookbook sent the agent to ``trigger::subscribe_to_trigger``, which has never been the
tool's name (``create_subscription``). Nothing checked, so nothing noticed. Two checks:

* every ``<toolset>::<tool>`` or ``<toolset>__<tool>`` in ``docs/agents/`` and ``docs/dev/`` (not the vision documents) whose toolset is built in
  here is a tool that toolset has. A name that ends in ``_`` is a documented stem (``find_<kind>``), not a tool;
* every ``METHOD /v1/...`` in ``docs/`` and ``AGENTS.md`` is an operation of the live OpenAPI schema, with ``{id}`` and ``<name>`` placeholders
  compared as one wildcard. Not checked, with the reason: the vision documents (``docs/dev/vision/``: the origin story and design
  philosophy, not a contract), placeholder paths the docs use to describe the factory (``/v1/{plural}``, ``/v1/x``), ``POST /v1/mcp`` (a mount
  outside the schema), and the test-mode ``/v1/_test/*`` routes.
"""

from __future__ import annotations

import asyncio
import re
from functools import lru_cache
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parents[2]
AGENT_DOCS = sorted(p for p in (REPO / "docs" / "agents").rglob("*.md") if not p.name.startswith("_"))
# The dev docs name tools too (``harness__list``, ``workspace_ext__invoke_graph``); the vision documents are not a contract.
DEV_DOCS = sorted(p for p in (REPO / "docs" / "dev").rglob("*.md") if "vision" not in p.relative_to(REPO / "docs" / "dev").parts)
TOOL_DOCS = [*AGENT_DOCS, *DEV_DOCS]


# ---- tools ----------------------------------------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _built_in_tools() -> dict[str, frozenset[str]]:
    """The tool names of the built-in toolsets that can be built without a running server: ``{toolset id: {bare tool names}}``."""
    from primer.api.registries import ProviderRegistry
    from primer.toolset.crud import build_crud_toolset
    from primer.toolset.harness import build_harness_toolset_provider
    from primer.toolset.misc import build_misc_toolset
    from primer.toolset.system import build_system_toolset
    from primer.toolset.trigger import build_trigger_toolset_provider
    from primer.toolset.web import build_web_toolset
    from primer.toolset.workspace_ext import build_workspace_ext_toolset
    from primer.toolset.workspaces import build_workspaces_toolset
    from tests.conftest import _FakeStorageProvider

    sp = _FakeStorageProvider()
    registry = ProviderRegistry(
        sp,
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    # Built with every optional collaborator wired (the real app wires them): a tool that exists only then, such as ``web::download``, which
    # needs a workspace registry, is a real tool. A name is stale only if no wiring has it.
    providers = {
        "system": build_system_toolset(
            storage_provider=sp,
            provider_registry=registry,
            semantic_search_registry=MagicMock(),
            workspace_registry=MagicMock(),
        ),
        "crud": build_crud_toolset(storage_provider=sp),
        "harness": build_harness_toolset_provider(storage_provider=sp),
        "trigger": build_trigger_toolset_provider(storage_provider=sp),
        "workspaces": build_workspaces_toolset(storage_provider=sp, workspace_registry=MagicMock()),
        "workspace_ext": build_workspace_ext_toolset(storage_provider=sp),
        "web": build_web_toolset(
            web_search_service=MagicMock(), web_fetch_service=MagicMock(), workspace_registry=MagicMock(),
        ),
        "misc": build_misc_toolset(),
    }

    async def names(toolset_id: str, provider) -> frozenset[str]:
        # The harness toolset registers its tools under scoped ids (``harness__list``, called ``harness::harness__list`` in the agent docs
        # and ``harness__list`` as a scoped id in the dev docs); the others use the bare name. Both spellings are the tool.
        ids = [tool.id async for tool in provider.list_tools()]
        return frozenset([*ids, *(tool_id.removeprefix(f"{toolset_id}__") for tool_id in ids)])

    async def collect() -> dict[str, frozenset[str]]:
        return {toolset_id: await names(toolset_id, provider) for toolset_id, provider in providers.items()}

    return asyncio.run(collect())


def _tool_refs_in(text: str) -> list[tuple[int, str, str]]:
    """``(line, toolset id, tool name)`` for every reference to a tool of a toolset that is built here."""
    toolsets = _built_in_tools()
    pattern = re.compile(r"\b(" + "|".join(sorted(toolsets)) + r")(?:::|__)([a-z][a-z0-9_]*)")
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for toolset_id, name in pattern.findall(line):
            if not name.endswith("_"):  # a stem such as ``find_``, written as ``find_<kind>``
                found.append((number, toolset_id, name))
    return found


def _tool_references(doc: Path) -> list[tuple[int, str, str]]:
    return _tool_refs_in(doc.read_text(encoding="utf-8"))


def test_the_built_in_toolsets_are_not_empty_so_the_check_cannot_pass_vacuously() -> None:
    tools = _built_in_tools()
    assert {"system", "workspaces", "trigger", "web"} <= set(tools)
    assert len(tools["system"]) > 50 and "set_reply_binding" in tools["system"]


@pytest.mark.parametrize("doc", TOOL_DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_docs_name_only_tools_that_exist(doc: Path) -> None:
    tools = _built_in_tools()
    missing = [
        f"  line {number}: {toolset_id}::{name}"
        for number, toolset_id, name in _tool_references(doc)
        if name not in tools[toolset_id]
    ]
    assert not missing, (
        f"{doc.relative_to(REPO)} names tools that the {sorted(tools)} toolsets do not have (an agent is told to call them):\n"
        + "\n".join(missing)
    )


def test_the_tool_reference_scan_finds_references_at_all() -> None:
    """The scan must see the references it is meant to check: a regex that stopped matching would pass every doc."""
    total = sum(len(_tool_references(doc)) for doc in AGENT_DOCS)
    assert total > 100, f"only {total} tool references found in docs/agents/"


# ---- endpoints ------------------------------------------------------------------------------------------------------------------

_ROUTE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/v1/[A-Za-z0-9_\-/{}<>.:*]+)")
_METHODS = ("get", "post", "put", "patch", "delete")

# Documents that describe intent, not the contract.
_NOT_A_CONTRACT = ("docs/dev/vision/",)
# ``POST /v1/mcp`` is a mount outside the OpenAPI schema; ``/v1/_test/*`` exists only when the test-endpoints switch is on.
_OUTSIDE_THE_SCHEMA = {("POST", "/v1/mcp")}
_OUTSIDE_THE_SCHEMA_PREFIXES = ("/v1/_test/",)


def _normalise(path: str) -> str:
    return re.sub(r"\{[^}]*\}|<[^>]*>", "{}", path).rstrip("/.,;:)`'\"")


def _is_a_placeholder(path: str) -> bool:
    """Paths the docs write to describe the factory (``/v1/{plural}``, ``/v1/x``, a truncated ``/v1/{llm``) and not an operation."""
    first = path.split("/")[2] if path.count("/") >= 2 else ""
    return path.count("{") != path.count("}") or first.startswith(("{", "<")) or first == "x"


@lru_cache(maxsize=1)
def _live_operations() -> frozenset[tuple[str, str]]:
    from fastapi import FastAPI

    from primer.api._app_routes import _mount_routers

    app = FastAPI()
    _mount_routers(app)
    paths = app.openapi()["paths"]
    return frozenset((method.upper(), _normalise(path)) for path, operations in paths.items() for method in operations if method in _METHODS)


def _doc_files() -> list[Path]:
    files = [REPO / "AGENTS.md", *sorted((REPO / "docs").rglob("*.md"))]
    return [f for f in files if f.exists() and "superpowers" not in f.parts]


def _route_refs_in(text: str) -> list[tuple[int, str, str]]:
    """``(line, METHOD, normalised path)`` for every endpoint a document names that the live schema is expected to have."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for method, path in _ROUTE.findall(line):
            normalised = _normalise(path)
            if _is_a_placeholder(normalised) or (method, normalised) in _OUTSIDE_THE_SCHEMA:
                continue
            if normalised.startswith(_OUTSIDE_THE_SCHEMA_PREFIXES):
                continue
            found.append((number, method, normalised))
    return found


def _route_references(doc: Path) -> list[tuple[int, str, str]]:
    if str(doc.relative_to(REPO)).startswith(_NOT_A_CONTRACT):
        return []
    return _route_refs_in(doc.read_text(encoding="utf-8"))


def test_the_live_schema_is_not_empty_so_the_route_check_cannot_pass_vacuously() -> None:
    live = _live_operations()
    assert len(live) > 200 and ("PUT", "/v1/workspaces/{}/reply_binding") in live


@pytest.mark.parametrize("doc", _doc_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_docs_name_only_endpoints_that_exist(doc: Path) -> None:
    live = _live_operations()
    missing = [f"  line {number}: {method} {path}" for number, method, path in _route_references(doc) if (method, path) not in live]
    assert not missing, f"{doc.relative_to(REPO)} names endpoints that are not in the OpenAPI schema:\n" + "\n".join(missing)


def test_the_route_reference_scan_finds_references_at_all() -> None:
    total = sum(len(_route_references(doc)) for doc in _doc_files())
    assert total > 80, f"only {total} endpoint references found in the docs"


# ---- the scans themselves -------------------------------------------------------------------------------------------------------


def test_both_spellings_of_a_scoped_tool_are_found() -> None:
    """``toolset::tool`` is how the docs write a tool, ``toolset__tool`` how an agent's ``tools`` list does; both are checked."""
    found = _tool_refs_in("call system::nope_one, then list system__nope_two in `tools`")

    assert [(toolset, name) for _, toolset, name in found] == [("system", "nope_one"), ("system", "nope_two")]


def test_a_documented_stem_and_a_toolset_not_built_here_are_not_references() -> None:
    assert _tool_refs_in("system::find_<kind> and system::get_ and search::search_agents and kb::anything") == []


def test_a_route_is_normalised_so_placeholder_names_do_not_matter() -> None:
    assert _route_refs_in("`PUT /v1/workspaces/{workspace_id}/reply_binding` and `PUT /v1/workspaces/<wid>/reply_binding`.") == [
        (1, "PUT", "/v1/workspaces/{}/reply_binding"),
        (1, "PUT", "/v1/workspaces/{}/reply_binding"),
    ]


def test_placeholders_the_mcp_mount_and_the_test_routes_are_not_expected_in_the_schema() -> None:
    text = "GET /v1/{plural}/{id} POST /v1/<resource> GET /v1/x POST /v1/mcp GET /v1/_test/anything GET /v1/{llm"

    assert _route_refs_in(text) == []


def test_an_invented_route_is_reported_with_its_line() -> None:
    assert _route_refs_in("fine\nDELETE /v1/nope/{id}.") == [(2, "DELETE", "/v1/nope/{}")]


def test_a_vision_document_is_not_a_contract() -> None:
    vision = next((REPO / "docs" / "dev" / "vision").glob("09-*.md"))

    assert _route_refs_in(vision.read_text(encoding="utf-8")), "the fixture must name a route, or this test proves nothing"
    assert _route_references(vision) == []
