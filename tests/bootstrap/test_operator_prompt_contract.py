"""The seeded prompts satisfy the core-kit contract clause by clause.

Prompt WORDING is expected to iterate. What may not drift is the contract:
the operator grounds itself in the platform wiki before claiming
anything, reaches every other capability through call_tool, walks the
decision ladder, coordinates sessions, follows the workspace
instructions, asks rather than guesses, and treats client tools as
best-effort; every grant the roster carries resolves to a real tool of
the built-in toolsets.
"""
from __future__ import annotations

from primer.bootstrap.operator_defaults import (
    BUILDER_PROMPT,
    BUILDER_TOOLS,
    CORE_KIT_TOOLS,
    EXPLORER_PROMPT,
    EXPLORER_TOOLS,
    OPERATOR_PROMPT,
    OPERATOR_TOOLS,
    PLANNER_PROMPT,
    PLANNER_TOOLS,
    SEEDED_AGENT_FACTORIES,
    TOOL_RUNNER_PROMPT,
    TOOL_RUNNER_TOOLS,
    WORKSPACE_INSTRUCTIONS_PROMPT_PART,
    builder_agent,
    operator_agent,
)
from tests.docs._live_toolsets import built_in_tools


def _operator_text() -> str:
    return "\n".join(OPERATOR_PROMPT).lower()


def _builder_text() -> str:
    return "\n".join(BUILDER_PROMPT).lower()


def test_operator_states_its_identity() -> None:
    assert "operator of this primer install" in _operator_text()


def test_operator_grounds_itself_in_the_wiki_first() -> None:
    text = _operator_text()
    assert all(w in text for w in ("wiki_read", "wiki_search", "call_tool"))
    assert "collection_tree" not in text and "read_document" not in text


def test_operator_carries_the_full_decision_ladder() -> None:
    text = _operator_text()
    for rung in ("answer directly", "invoke_agent", "invoke_graph", "wiki"):
        assert rung in text, rung
    assert "'builder'" in text


def test_operator_asks_instead_of_guessing() -> None:
    text = _operator_text()
    assert "ask_user" in text
    assert "destructive" in text


def test_operator_treats_client_tools_as_best_effort() -> None:
    text = _operator_text()
    assert "open_file" in text and "inform_user" in text
    assert "best-effort" in text


def test_operator_uses_switch_binding_not_switch_to_agent() -> None:
    text = _operator_text()
    assert "switch_binding" in text
    assert "switch_to_agent" not in text


def test_operator_plans_before_multi_step_work() -> None:
    text = _operator_text()
    assert "'planner'" in text, "the ladder must name the planner rung"
    assert "one-paragraph context digest" in text


def test_operator_upgrades_long_horizon_plans_to_graphs() -> None:
    text = _operator_text()
    assert "ask the builder for a graph" in text


def test_operator_follows_workspace_instructions() -> None:
    assert WORKSPACE_INSTRUCTIONS_PROMPT_PART in OPERATOR_PROMPT


def test_operator_coordinates_sessions() -> None:
    text = _operator_text()
    assert "list_my_sessions" in text and "message_session" in text


def test_builder_reads_before_it_writes_and_expects_the_gate() -> None:
    text = _builder_text()
    assert all(w in text for w in ("wiki", "call_tool", "approval"))


def test_the_core_kit_is_the_default_grant() -> None:
    assert OPERATOR_TOOLS == CORE_KIT_TOOLS + (
        "workspaces__list_my_sessions",
        "workspaces__message_session",
    )
    for tools in (BUILDER_TOOLS, PLANNER_TOOLS, EXPLORER_TOOLS,
                  TOOL_RUNNER_TOOLS):
        assert tools == CORE_KIT_TOOLS
    assert not any(
        t.startswith(("crud__", "collections__", "search__"))
        for t in OPERATOR_TOOLS + BUILDER_TOOLS
    )


def test_every_seeded_grant_is_a_real_tool() -> None:
    """The P1/P3 gate: every grant the roster ships resolves to a live tool."""
    live = built_in_tools()  # tests.docs._live_toolsets
    for agent_id, factory in SEEDED_AGENT_FACTORIES.items():
        for scoped in factory("p").tools:
            toolset, _, name = scoped.partition("__")
            assert name in live[toolset], (agent_id, scoped)


def test_the_seeded_rows_carry_the_prompts_and_grants() -> None:
    operator = operator_agent("llm-1--qwen")
    builder = builder_agent("llm-1--qwen")
    assert operator.system_prompt == list(OPERATOR_PROMPT)
    assert builder.system_prompt == list(BUILDER_PROMPT)
    assert operator.tools == list(OPERATOR_TOOLS)
    assert builder.tools == list(BUILDER_TOOLS)


def test_every_base_description_is_an_advertisement() -> None:
    """Does X. Use when Y. Returns Z. - the searchable surface."""
    from primer.bootstrap.operator_defaults import (
        BUILDER_DESCRIPTION,
        EXPLORER_DESCRIPTION,
        OPERATOR_DESCRIPTION,
        PLANNER_DESCRIPTION,
        TOOL_RUNNER_DESCRIPTION,
    )
    for name, desc in {
        "operator": OPERATOR_DESCRIPTION,
        "builder": BUILDER_DESCRIPTION,
        "planner": PLANNER_DESCRIPTION,
        "explorer": EXPLORER_DESCRIPTION,
        "tool-runner": TOOL_RUNNER_DESCRIPTION,
    }.items():
        assert "Use when" in desc, name
        assert "Returns" in desc, name


def test_builder_knows_how_to_emit_a_graph() -> None:
    text = " ".join(BUILDER_PROMPT)
    assert "input_template" in text, (
        "the builder must know an agent node's input field by name"
    )
    assert "static" in text and "begin" in text.lower()


# ---- the specialist prompts ------------------------------------------------


def test_planner_grounds_its_plans_and_never_executes() -> None:
    text = "\n".join(PLANNER_PROMPT).lower()
    assert "wiki_search" in text
    assert "never execute" in text


def test_explorer_reads_but_never_mutates() -> None:
    text = "\n".join(EXPLORER_PROMPT).lower()
    assert "wiki_search" in text and "wiki_read" in text
    assert "never create, edit or delete" in text


def test_tool_runner_names_only_tools_it_has_seen() -> None:
    text = "\n".join(TOOL_RUNNER_PROMPT).lower()
    assert "wiki_search" in text
    assert "describe_tool" in text
    assert "never guess a tool id" in text
