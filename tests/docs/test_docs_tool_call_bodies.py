"""Every example tool call in the agent docs and the platform wiki passes the arguments its tool's input schema accepts (follow-up to finding A-19).

``docs/agents/`` is served to agents by search, and its examples are copied into tool calls. Checking only that a documented tool EXISTS (see
``test_docs_name_real_tools_and_routes.py``) missed examples whose arguments the tool refuses: a matcher written ``"surface": "channel"`` where the
schema wants a list (``["channel"]``), an agent passed without its ``entity`` wrapper, an agent whose ``model`` was still the retired provider/model
pair. This validates the arguments of every documented call against the live tool's ``args_schema`` with ``jsonschema``.

Three shapes of documented call are read:

* a JSON block with ``{"tool": "<toolset>::<tool>", "arguments": {...}}`` (``channels.md`` and others);
* the cookbook shape, an inline-code line ``<toolset>::<tool>`` followed immediately by a JSON block, which is the arguments;
* the wiki shape, a JSON block with ``{"toolset_id": "<toolset>", "tool_name": "<tool>", "arguments": {...}}``: a ``call_tool`` call, the way the
  ``docs/wiki/`` pages and cookbooks show every call.

Every fenced block is paired with its own closing fence whatever its language, so a ``text`` or ``bash`` block cannot hide the calls after it.

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
WIKI_DOCS = sorted(p for p in (REPO / "docs" / "wiki").rglob("*.md") if not p.name.startswith("_"))
DOC_FILES = [*AGENT_DOCS, *WIKI_DOCS]

# Every fenced block, whatever its info string (``json``, ``text``, ``bash``, none): a fence must be paired with its own closing fence, or a
# ```text block shifts every later pair by one and the calls after it go unread. Only the ``json`` and unlabelled blocks are parsed.
_FENCE = re.compile(r"```(?P<info>[^\n`]*)\n(?P<body>.*?)```", re.DOTALL)
_JSON_INFO = ("", "json")
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
    """Every documented call to a tool of a built-in toolset, in the three shapes the docs use."""
    found: list[Call] = []
    for match in _PROSE_CALL.finditer(text):
        try:
            found.append(Call(match["toolset"], match["tool"], json.loads(match["body"])))
        except ValueError:
            continue  # not JSON (an ellipsis, a comment)
    for fence in _FENCE.finditer(text):
        if fence["info"].strip() not in _JSON_INFO:
            continue  # a text / shell / python block: not a call
        try:
            block = json.loads(fence["body"])
        except ValueError:
            continue
        for obj in _objects(block):
            tool = obj.get("tool")
            if isinstance(tool, str) and "::" in tool and isinstance(obj.get("arguments"), dict):
                toolset, _, name = tool.partition("::")
                found.append(Call(toolset, name, obj["arguments"]))
            elif (
                isinstance(obj.get("toolset_id"), str)
                and isinstance(obj.get("tool_name"), str)
                and isinstance(obj.get("arguments"), dict)
            ):
                found.append(Call(obj["toolset_id"], obj["tool_name"], obj["arguments"]))
    return found


def problems_in(call: Call) -> list[str]:
    """What the live tool's input schema refuses in the call's arguments, with placeholders accepted."""
    tool = built_in_tools().get(call.toolset, {}).get(call.tool)
    if tool is None or not tool.args_schema:
        return []  # an unknown tool name is test_docs_name_real_tools_and_routes' finding; this test only checks arguments
    validator = jsonschema.Draft202012Validator(tool.args_schema)
    problems = []
    # A tool's argument model ignores a name it does not declare, and the schema does not forbid one, so a misspelt or invented argument
    # (the harness register example passed an ``id`` and ``overrides`` that the tool never read) validates. Top-level names must be declared.
    declared = tool.args_schema.get("properties")
    if isinstance(declared, dict) and tool.args_schema.get("additionalProperties") is not True and isinstance(call.arguments, dict):
        problems.extend(f"{call.label()} {name}: not an argument of this tool" for name in call.arguments if name not in declared)
    for error in validator.iter_errors(call.arguments):
        if isinstance(error.instance, str) and _PLACEHOLDER.match(error.instance):
            continue
        where = "/".join(str(part) for part in error.absolute_path) or "(arguments)"
        problems.append(f"{call.label()} {where}: {error.message[:160]}")
    return problems


def test_the_scan_finds_enough_documented_calls_to_be_real() -> None:
    calls = [call for doc in DOC_FILES for call in calls_in(doc.read_text(encoding="utf-8"))]
    checked = [c for c in calls if built_in_tools().get(c.toolset, {}).get(c.tool) is not None]

    assert len(checked) >= 60, f"only {len(checked)} documented calls to a built-in tool found in docs/agents/ and docs/wiki/"


def test_every_built_in_toolset_has_tools_and_schemas() -> None:
    """A toolset that built empty, or whose tools carry no schema, would make every call to it pass unchecked."""
    for toolset_id, tools in built_in_tools().items():
        assert tools, f"the {toolset_id} toolset built with no tools"
        assert any(t.args_schema for t in tools.values()), f"no tool of the {toolset_id} toolset carries an args_schema"


@pytest.mark.parametrize("doc", DOC_FILES, ids=lambda p: str(p.relative_to(REPO)))
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


def test_the_call_tool_shape_is_read() -> None:
    """The wiki's shape: a json object with ``toolset_id``, ``tool_name`` and an ``arguments`` object is a ``call_tool`` call."""
    text = (
        '```json\n{"toolset_id": "web", "tool_name": "web_search", "arguments": {"query": "x"}}\n```\n'
        '```json\n{"toolset_id": "workspaces", "tool_name": "create_workspace", '
        '"arguments": {"template_id": "py-base"}}\n```\n'
    )

    assert [(c.toolset, c.tool) for c in calls_in(text)] == [("web", "web_search"), ("workspaces", "create_workspace")]


def test_a_fence_in_another_language_does_not_hide_the_calls_after_it() -> None:
    """A ```text (or ```bash) block before a call must still be read as one fenced block. Paired as anything else, its CLOSING fence
    opens a "block" that ends at the next call's opening fence, every later fence is read off by one, and no call after it is checked."""
    text = (
        "```text\nhttp-request refused: ...\n```\n\nThen:\n\n"
        '```json\n{"tool": "system::delete_agent", "arguments": {"id": "a"}}\n```\n\n'
        '```json\n{"ok": true}\n```\n\n'
        '```json\n{"tool": "system::get_agent", "arguments": {"id": "a"}}\n```\n'
    )

    assert [(c.toolset, c.tool) for c in calls_in(text)] == [("system", "delete_agent"), ("system", "get_agent")]


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


def test_an_argument_the_tool_does_not_declare_is_refused() -> None:
    """The harness register example passed an ``id`` and ``overrides`` that the tool never reads: its schema does not forbid extra names,
    so only the declared-names rule sees them."""
    call = Call(
        "harness", "harness__register",
        {"name": "n", "slug": "my-slug", "git_url": "u", "id": "h-1", "overrides": {"a": 1}},
    )

    problems = problems_in(call)

    assert any("id: not an argument" in p for p in problems) and any("overrides: not an argument" in p for p in problems), problems
    assert problems_in(Call("harness", "harness__register", {"name": "n", "slug": "my-slug", "git_url": "u"})) == []


def test_a_web_search_call_with_max_results_is_refused() -> None:
    """C6 pin: the web_search schema declares no ``max_results``, so the declared-names rule refuses it."""
    call = Call("web", "web_search", {"query": "x", "max_results": 5})
    problems = problems_in(call)

    assert any("max_results" in p and "not an argument" in p for p in problems), problems
