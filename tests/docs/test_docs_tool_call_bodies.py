"""Every example tool call in the agent docs passes the arguments its tool's input schema accepts (follow-up to finding A-19).

``docs/agents/`` is served to agents by search, and its examples are copied into tool calls. Checking only that a documented tool EXISTS (see
``test_docs_name_real_tools_and_routes.py``) missed examples whose arguments the tool refuses: a matcher written ``"surface": "channel"`` where the
schema wants a list (``["channel"]``), an agent passed without its ``entity`` wrapper, an agent whose ``model`` was still the retired provider/model
pair. This validates the arguments of every documented call against the live tool's ``args_schema`` with ``jsonschema``.

Two shapes of documented call are read:

* a JSON block with ``{"tool": "<toolset>::<tool>", "arguments": {...}}`` (``channels.md`` and others);
* the cookbook shape, an inline-code line ``<toolset>::<tool>`` followed immediately by a JSON block, which is the arguments.

Only calls to a built-in toolset's existing tool are validated (a missing tool is the other test's finding). A string that is a placeholder
(``<id>``, ``...``) is accepted wherever it stands, since a doc may write ``<n>`` for a number. What this does NOT check: the values (a profile
id that exists, a trigger id that was returned by an earlier call); the Response blocks; calls to toolsets that are not built here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from tests.docs._live_toolsets import built_in_tools

REPO = Path(__file__).resolve().parents[2]
AGENT_DOCS = sorted(p for p in (REPO / "docs" / "agents").rglob("*.md") if not p.name.startswith("_"))

_FENCE = re.compile(r"```(?:json)?[ \t]*\n(.*?)```", re.DOTALL)
_PROSE_CALL = re.compile(r"`(?P<toolset>[a-z_]+)::(?P<tool>[a-z0-9_]+)`[ \t]*\n```json[ \t]*\n(?P<body>.*?)```", re.DOTALL)
_PLACEHOLDER = re.compile(r"^(<[^<>]*>|\.\.\.)$")


@dataclass(frozen=True)
class Call:
    toolset: str
    tool: str
    arguments: Any

    def label(self) -> str:
        return f"{self.toolset}::{self.tool}"


def _objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def calls_in(text: str) -> list[Call]:
    """Every documented call to a tool of a built-in toolset, in the two shapes the docs use."""
    found: list[Call] = []
    for match in _PROSE_CALL.finditer(text):
        try:
            found.append(Call(match["toolset"], match["tool"], json.loads(match["body"])))
        except ValueError:
            continue  # not JSON (an ellipsis, a comment)
    for body in _FENCE.findall(text):
        try:
            block = json.loads(body)
        except ValueError:
            continue
        for obj in _objects(block):
            tool = obj.get("tool")
            if isinstance(tool, str) and "::" in tool and isinstance(obj.get("arguments"), dict):
                toolset, _, name = tool.partition("::")
                found.append(Call(toolset, name, obj["arguments"]))
    return found


def problems_in(call: Call) -> list[str]:
    """What the live tool's input schema refuses in the call's arguments, with placeholders accepted."""
    tool = built_in_tools().get(call.toolset, {}).get(call.tool)
    if tool is None or not tool.args_schema:
        return []
    validator = jsonschema.Draft202012Validator(tool.args_schema)
    problems = []
    for error in validator.iter_errors(call.arguments):
        if isinstance(error.instance, str) and _PLACEHOLDER.match(error.instance):
            continue
        where = "/".join(str(part) for part in error.absolute_path) or "(arguments)"
        problems.append(f"{call.label()} {where}: {error.message[:160]}")
    return problems


def test_the_scan_finds_enough_documented_calls_to_be_real() -> None:
    calls = [call for doc in AGENT_DOCS for call in calls_in(doc.read_text(encoding="utf-8"))]
    checked = [c for c in calls if built_in_tools().get(c.toolset, {}).get(c.tool) is not None]

    assert len(checked) >= 60, f"only {len(checked)} documented calls to a built-in tool found in docs/agents/"


def test_every_built_in_toolset_has_tools_and_schemas() -> None:
    """A toolset that built empty, or whose tools carry no schema, would make every call to it pass unchecked."""
    for toolset_id, tools in built_in_tools().items():
        assert tools, f"the {toolset_id} toolset built with no tools"
        assert any(t.args_schema for t in tools.values()), f"no tool of the {toolset_id} toolset carries an args_schema"


@pytest.mark.parametrize("doc", AGENT_DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_every_documented_call_passes_its_tools_input_schema(doc: Path) -> None:
    problems = [p for call in calls_in(doc.read_text(encoding="utf-8")) for p in problems_in(call)]

    assert not problems, f"{doc.relative_to(REPO)} shows tool calls their tools would refuse:\n" + "\n".join(f"  {p}" for p in problems)


# ---- the scan itself ------------------------------------------------------------------------------------------------------------


def test_both_shapes_of_a_documented_call_are_read() -> None:
    text = (
        '`system::create_channel`\n```json\n{"entity": {"id": "c"}}\n```\n'
        '```json\n{"tool": "system::delete_agent", "arguments": {"id": "a"}}\n```\n'
    )

    assert [(c.toolset, c.tool) for c in calls_in(text)] == [("system", "create_channel"), ("system", "delete_agent")]


def test_a_string_where_the_schema_wants_a_list_is_refused() -> None:
    call = Call(
        "system", "create_channel_binding",
        {
            "trigger_id": "tr-1", "event_matcher": {"event_type": "message.posted", "surface": "channel"},
            "config": {"kind": "agent_fresh_session", "workspace_id": "w", "agent_id": "a"},
        },
    )

    assert any("surface" in p for p in problems_in(call))


def test_a_placeholder_is_accepted_and_a_missing_required_argument_is_not() -> None:
    ok = Call("system", "get_agent", {"id": "<agent id>"})
    missing = Call("system", "create_agent", {"id": "a", "description": "d"})

    assert problems_in(ok) == []
    assert any("entity" in p for p in problems_in(missing))


def test_a_call_to_a_toolset_not_built_here_is_not_checked() -> None:
    assert problems_in(Call("deploy-tools", "deploy_prod", {"anything": 1})) == []
