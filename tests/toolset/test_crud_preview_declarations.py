"""What an approval card for each DEFAULT-GATED ``crud`` tool may show (design note 01a11cd3-66b0, slice 1).

``bootstrap/seed.py`` seeds a ``required`` approval policy for each tool of ``CRUD_TOOL_NAMES`` (the builder agent's platform mutations), so these nine are the tools whose cards a
fresh install actually shows. Each takes ONE nested body (``entity``) or a few flat arguments, so the default rule (only a boolean, number or enum is shown) would leave the card
with nothing a person can decide on. Each declares the paths that name WHAT is being created or changed, and withholds the free text that can hold a pasted secret: an agent's
system and compaction prompts, a graph node's templates and arguments, a webhook trigger's token and HMAC secret, a Python toolset's source.

The declarations are built into the toolset (``primer/toolset/crud.py`` ``PREVIEW_ARGS``, checked against each tool's schema when it is built); this file pins that every default-gated
tool declares, that every declared path is real, and what a card draws for a realistic call.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.api.routers.workspaces import _approval_preview
from primer.common.preview_paths import missing_paths
from primer.toolset.crud import CRUD_TOOL_NAMES, build_crud_toolset

SECRET = "Zq7!pLm2$vXr9&kTe4"
SECRET_2 = "correct horse battery staple Zq7"


@pytest.fixture(scope="module")
def tools() -> dict:
    import asyncio

    async def collect():
        provider = build_crud_toolset(storage_provider=SimpleNamespace(get_storage=lambda model: None))  # type: ignore[arg-type]
        return {tool.id: tool async for tool in provider.list_tools()}

    return asyncio.run(collect())


def _card(tools, tool_id: str, arguments: dict) -> dict:
    tool = tools[tool_id]
    return _approval_preview({"name": f"crud__{tool_id}", "arguments": arguments}, {"paths": list(tool.preview_args), "source": "tool"})


# ---- every default-gated tool declares, and every path is real -----------------------------------------------------------------------------------------------


def test_every_default_gated_crud_tool_declares_what_its_card_may_show(tools) -> None:
    undeclared = [name for name in CRUD_TOOL_NAMES if tools[name].preview_args is None]

    assert not undeclared, f"these seeded-gated tools would show only booleans and enums on their cards: {undeclared}"


@pytest.mark.parametrize("name", CRUD_TOOL_NAMES)
def test_every_declared_path_names_an_argument_of_the_tool(tools, name: str) -> None:
    tool = tools[name]

    assert tool.preview_args and missing_paths(tool.args_schema, tool.preview_args) == []


# ---- agents ------------------------------------------------------------------------------------------------------------------------------------------------------


AGENT = {
    "id": "reviewer", "description": "Reviews diffs", "model": {"profile_id": "anthropic--sonnet"}, "tools": ["system__get_agent"],
    "system_prompt": [f"You are a reviewer. The deploy key is {SECRET}."], "compaction_prompt": [f"Summarise. Also remember {SECRET_2}"],
    "compaction_tool_access": False, "allow_external_tools": False,
}


@pytest.mark.parametrize("name", ["create_agent", "update_agent"])
def test_an_agent_card_names_the_agent_and_withholds_its_prompts(tools, name: str) -> None:
    arguments = {"entity": AGENT, **({"id": "reviewer"} if name == "update_agent" else {})}

    card = _card(tools, name, arguments)

    drawn = card["arguments"] + " " + " ".join(card["hidden_keys"])
    assert "Zq7" not in drawn and "horse battery" not in drawn and "deploy key" not in drawn
    assert card["hidden_keys"] == ["entity.system_prompt", "entity.compaction_prompt"]
    assert '"id": "reviewer"' in card["arguments"] and '"description": "Reviews diffs"' in card["arguments"]
    assert card["truncated"] is True and card["preview"] == "tool"


# ---- graphs ------------------------------------------------------------------------------------------------------------------------------------------------------


GRAPH = {
    "id": "pipeline", "description": "a pipeline",
    "nodes": [
        {"kind": "begin", "id": "begin"},
        {"kind": "agent", "id": "a1", "agent_id": "reviewer", "input_template": f"Use the key {SECRET}"},
        {"kind": "tool_call", "id": "t1", "tool_id": "system__get_agent", "arguments": {"id": SECRET_2}, "arguments_template": {"id": "{{ x }}"}},
        {"kind": "end", "id": "end", "output_template": f"done {SECRET}"},
    ],
    "edges": [{"kind": "static", "from_node": "begin", "to_node": "a1"}],
}


@pytest.mark.parametrize("name", ["create_graph", "update_graph"])
def test_a_graph_card_names_the_nodes_and_withholds_their_templates_and_arguments(tools, name: str) -> None:
    arguments = {"entity": GRAPH, **({"id": "pipeline"} if name == "update_graph" else {})}

    card = _card(tools, name, arguments)

    drawn = card["arguments"] + " " + " ".join(card["hidden_keys"])
    assert "Zq7" not in drawn and "horse battery" not in drawn
    assert set(card["hidden_keys"]) == {"entity.nodes.input_template", "entity.nodes.arguments", "entity.nodes.arguments_template", "entity.nodes.output_template"}
    assert "reviewer" in card["arguments"] or "a1" in card["arguments"], "the card still says which nodes there are"


# ---- triggers ----------------------------------------------------------------------------------------------------------------------------------------------------


def test_a_webhook_trigger_card_never_draws_its_token_or_hmac_secret(tools) -> None:
    arguments = {
        "slug": "deploy-hook", "name": "Deploy hook", "description": "fires on deploy", "enabled": True,
        "config": {"kind": "webhook", "token": SECRET, "hmac_secret": SECRET_2, "interactive": False, "wait_timeout_seconds": 30},
    }

    card = _card(tools, "create_trigger", arguments)

    drawn = card["arguments"] + " " + " ".join(card["hidden_keys"])
    assert "Zq7" not in drawn and "horse battery" not in drawn
    assert card["hidden_keys"] == ["config.token", "config.hmac_secret"]
    assert "slug=deploy-hook" in card["arguments"] and "enabled=true" in card["arguments"]


def test_a_schedule_trigger_card_shows_when_it_fires(tools) -> None:
    arguments = {"id": "tr-1", "enabled": False, "config": {"kind": "scheduled", "cron": "0 9 * * 1-5", "timezone": "Europe/Berlin", "catchup": False}}

    card = _card(tools, "update_trigger", arguments)

    assert "0 9 * * 1-5" in card["arguments"] and "Europe/Berlin" in card["arguments"] and card["hidden_keys"] == []


# ---- python toolsets ----------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["create_python_toolset", "update_python_toolset_source"])
def test_a_python_toolset_card_names_the_toolset_and_never_draws_the_source(tools, name: str) -> None:
    source = f'@primer_tool()\ndef leak():\n    """x"""\n    return "{SECRET}"\n'

    card = _card(tools, name, {"toolset_id": "my-tools", "source": source, "default_timeout_seconds": 30} if name == "create_python_toolset" else {"toolset_id": "my-tools", "source": source})

    assert "toolset_id=my-tools" in card["arguments"] and "source=<hidden>" in card["arguments"]
    assert "Zq7" not in card["arguments"] and card["hidden_keys"] == ["source"]


def test_list_python_tools_shows_its_one_argument(tools) -> None:
    card = _card(tools, "list_python_tools", {"toolset_id": "my-tools"})

    assert card["arguments"] == "toolset_id=my-tools" and card["truncated"] is False
