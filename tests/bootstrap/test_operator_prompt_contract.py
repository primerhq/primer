"""S5 P4: the seeded prompts satisfy spec sections 6 and 7 clause by clause.

Prompt WORDING is expected to iterate. What may not drift is the contract:
the operator grounds itself in the system collection before claiming
anything, walks the decision ladder, asks rather than guesses, and treats
client tools as best-effort; the builder reads before writing, expects the
approval gate, and holds the crud toolset the operator does not.
"""
from __future__ import annotations

from primer.bootstrap.operator_defaults import (
    BUILDER_PROMPT,
    BUILDER_TOOLS,
    OPERATOR_PROMPT,
    OPERATOR_TOOLS,
    builder_agent,
    operator_agent,
)
from primer.toolset.crud import CRUD_TOOL_NAMES


def _operator_text() -> str:
    return "\n".join(OPERATOR_PROMPT).lower()


def _builder_text() -> str:
    return "\n".join(BUILDER_PROMPT).lower()


def test_operator_states_its_identity() -> None:
    assert "operator of this primer install" in _operator_text()


def test_operator_grounds_itself_in_the_system_collection_first() -> None:
    text = _operator_text()
    assert "system collection" in text
    for tool in ("collection_tree", "read_document", "search"):
        assert tool in text, tool


def test_operator_carries_the_full_decision_ladder() -> None:
    text = _operator_text()
    for rung in ("answer directly", "invoke_agent", "invoke_graph", "how-to"):
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


def test_builder_reads_before_it_writes_and_expects_the_gate() -> None:
    text = _builder_text()
    assert "/how-to" in text
    assert "approval" in text


def test_the_grants_match_the_spec_split() -> None:
    assert not any(t.startswith("crud__") for t in OPERATOR_TOOLS)
    assert {f"crud__{n}" for n in CRUD_TOOL_NAMES} <= set(BUILDER_TOOLS)
    # Amendment C7: entity discovery is grep/tree over the system collection,
    # never the dissolved internal-collections search toolset.
    assert not any(t.startswith("search__") for t in OPERATOR_TOOLS)
    assert not any(t.startswith("search__") for t in BUILDER_TOOLS)


def test_the_seeded_rows_carry_the_prompts_and_grants() -> None:
    operator = operator_agent("llm-1--qwen")
    builder = builder_agent("llm-1--qwen")
    assert operator.system_prompt == list(OPERATOR_PROMPT)
    assert builder.system_prompt == list(BUILDER_PROMPT)
    assert operator.tools == list(OPERATOR_TOOLS)
    assert builder.tools == list(BUILDER_TOOLS)
