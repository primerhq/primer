"""Reference pages: one generated page per tool, plus the index pages above them.

Pivot P1 task 9. The pages are built from tool entries (see ``_entries.py``); the
production-shape sweep at the end renders every tool of the built-in toolsets.
"""
from __future__ import annotations

import json
import logging
import re

import pytest

from primer.knowledge.wiki.build import by_purpose_rows
from primer.knowledge.wiki.pages import SUMMARY_MAX, WikiSourceError
from primer.knowledge.wiki.reference import (
    REFERENCE_ROOT,
    REFERENCE_TOOLS,
    call_example,
    reference_pages,
    reference_path,
)
from primer.model.chat import Tool, ToolExample
from tests.knowledge.wiki._entries import (
    OBJECT_SCHEMA,
    StandInEntry,
    entry_from_tool,
    make_entry,
    real_entries,
)

DASH, ELLIPSIS, E_ACUTE = chr(0x2014), chr(0x2026), chr(0xE9)

SIX_HEADINGS = [
    "## What it does", "## Call it", "## Arguments", "## Facts", "## Examples", "## Used in",
]


def _headings(body: str) -> list[str]:
    return [line for line in body.splitlines() if line.startswith("## ")]


def _json_blocks(body: str) -> list[object]:
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", body, flags=re.S)]


def _bare_entry(description: str, *, toolset_id: str = "misc", tool_id: str = "t") -> StandInEntry:
    """An entry whose description is exactly the text given (not built by make_tool)."""
    tool = Tool(
        id=tool_id, toolset_id=toolset_id, description=description,
        args_schema={"type": "object"}, required_role="user",
    )
    return StandInEntry(toolset_id, tool, "user", False, False, None)


# ---- paths and call examples ----------------------------------------------------


def test_reference_paths():
    assert (REFERENCE_ROOT, REFERENCE_TOOLS) == ("reference", "reference/tools")
    assert reference_path("misc", "get_datetime") == "reference/tools/misc/get_datetime"


def test_call_example_goes_through_call_tool_for_a_toolset_tool():
    entry = make_entry(
        "misc", "get_datetime", schema=OBJECT_SCHEMA,
        examples=[ToolExample(args={"tz": "UTC"}), ToolExample(args={})],
    )
    example = call_example(entry)
    assert example == {"toolset_id": "misc", "tool_name": "get_datetime", "arguments": {"tz": "UTC"}}
    assert list(example) == ["toolset_id", "tool_name", "arguments"]


def test_call_example_is_a_direct_call_for_a_session_tool():
    entry = make_entry(
        "workspace", "read", needs_workspace=True,
        schema={"type": "object", "properties": {"path": {"type": "string"}}},
        examples=[ToolExample(args={"path": "README.md"})],
    )
    assert call_example(entry) == {"name": "workspace__read", "arguments": {"path": "README.md"}}


def test_a_tool_without_examples_is_called_with_empty_arguments():
    entry = make_entry("misc", "ping", examples=[])
    assert call_example(entry)["arguments"] == {}


def test_a_tool_without_examples_gets_the_minimal_arguments_its_schema_requires():
    """A generated route tool carries no examples: its page shows the minimal call its schema accepts (a typed
    placeholder per required property), so the example always validates against the tool's own schema."""
    import jsonschema

    entry = make_entry(
        "misc", "t",
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "count": {"type": "integer", "minimum": 1},
                "kind": {"const": "predicate"},
                "body": {"$ref": "#/$defs/body"},
            },
            "required": ["name", "count", "kind", "body"],
            "$defs": {"body": {"type": "object", "properties": {"model": {"type": "string"}}, "required": ["model"]}},
        },
        examples=[],
    )
    example = call_example(entry)
    assert example["arguments"] == {"name": "x", "count": 1, "kind": "predicate", "body": {"model": ""}}
    jsonschema.validate(example["arguments"], entry.tool.args_schema)


def test_a_one_of_union_without_examples_is_named_in_its_discriminator():
    """A discriminated ``oneOf`` the tool gives no example for: the placeholder names the branch in the
    discriminator field (the branch literal), so the value passes exactly one branch."""
    import jsonschema

    entry = make_entry(
        "misc", "t",
        schema={
            "type": "object",
            "properties": {
                "page": {
                    "oneOf": [{"$ref": "#/$defs/offset"}, {"$ref": "#/$defs/cursor"}],
                    "discriminator": {"propertyName": "kind"},
                },
            },
            "required": ["page"],
            "$defs": {
                "offset": {
                    "type": "object",
                    "properties": {"kind": {"const": "offset"}, "length": {"type": "integer"}},
                    "required": ["length"],
                },
                "cursor": {
                    "type": "object",
                    "properties": {"kind": {"const": "cursor"}, "cursor": {"type": "string"}},
                    "required": ["cursor"],
                },
            },
        },
        examples=[],
    )
    example = call_example(entry)
    assert example["arguments"] == {"page": {"length": 0, "kind": "offset"}}
    jsonschema.validate(example["arguments"], entry.tool.args_schema)


def test_every_live_tool_reference_call_example_validates_against_its_own_schema():
    """The corpus check that pins the minimal-arguments fallback against the whole live catalogue: the call
    example rendered for every tool (curated or generated) validates against that tool's own ``args_schema``.
    Before the ``_minimal_arguments`` fix this red on 206 of the 496 pages - the generated route tools carry
    no curated examples, so their pages emitted ``{}`` against schemas with required arguments."""
    import jsonschema

    from tests.docs._wiki import tool_entries

    entries = tool_entries()
    assert entries, "the live catalogue must not be empty"
    bad = []
    for entry in entries:
        try:
            jsonschema.validate(call_example(entry)["arguments"], entry.tool.args_schema)
        except jsonschema.ValidationError as exc:
            bad.append(f"{entry.scoped_id}: {exc.message}")
    assert bad == [], "call examples that do not validate against their own schema: " + "; ".join(bad[:10])


def test_call_example_hands_out_a_copy_of_the_arguments():
    entry = make_entry(
        "misc", "t", schema={"type": "object"}, examples=[ToolExample(args={"a": {"b": 1}})],
    )
    call_example(entry)["arguments"]["a"]["b"] = 2
    assert entry.tool.examples[0].args == {"a": {"b": 1}}


# ---- one tool page ----------------------------------------------------------------


GOLDEN = """\
# misc__get_datetime

## What it does

Return the current date and time.

Use when you need to know the time.

## Call it

```json
{
  "toolset_id": "misc",
  "tool_name": "get_datetime",
  "arguments": {}
}
```

Call it through system__call_tool.

## Arguments

```json
{
  "additionalProperties": false,
  "properties": {
    "tz": {
      "type": "string"
    }
  },
  "type": "object"
}
```

## Facts

- Scoped id: misc__get_datetime
- Role: user
- Approval by default: none
- Pauses the turn: no
- Needs a workspace: no

## Examples

- `{}` -> the ISO time
- `{"tz":"UTC"}` -> a zoned time (pins the zone)
- `{"tz":"Europe/Paris"}` (no returns line)

## Used in

- [start-here/calling-tools](start-here/calling-tools)
"""


def test_a_tool_page_has_exactly_this_shape():
    entry = make_entry(
        "misc", "get_datetime",
        purpose="Return the current date and time.",
        when="Use when you need to know the time.",
        examples=[
            ToolExample(args={}, returns="the ISO time"),
            ToolExample(args={"tz": "UTC"}, returns="a zoned time", note="pins the zone"),
            ToolExample(args={"tz": "Europe/Paris"}, note="no returns line"),
        ],
    )
    pages = reference_pages(
        [entry], used_in={"reference/tools/misc/get_datetime": ["start-here/calling-tools"]},
    )
    assert pages["reference/tools/misc/get_datetime"].body == GOLDEN


def test_a_tool_page_meta_holds_the_documented_keys():
    entry = make_entry(
        "crud", "create_agent", role="admin", pauses=False, needs_workspace=False, gate="required",
    )
    page = reference_pages([entry], used_in={})["reference/tools/crud/create_agent"]
    assert page.kind == "reference" and page.title == "crud__create_agent"
    assert page.path == "reference/tools/crud/create_agent"
    assert page.meta == {
        "kind": "reference",
        "summary": "Do the thing.",
        "toolset": "crud",
        "tool": "create_agent",
        "scoped_id": "crud__create_agent",
        "role": "admin",
        "pauses_turn": False,
        "needs_workspace": False,
        "approval_by_default": "required",
        "call": "call_tool",
    }
    assert page.summary == "Do the thing."


@pytest.mark.parametrize(
    "role, pauses, needs_ws, gate, facts",
    [
        ("user", False, False, None,
         ["- Role: user", "- Approval by default: none", "- Pauses the turn: no", "- Needs a workspace: no"]),
        ("admin", True, True, "required",
         ["- Role: admin", "- Approval by default: required", "- Pauses the turn: yes", "- Needs a workspace: yes"]),
    ],
)
def test_the_facts_follow_the_entry(role, pauses, needs_ws, gate, facts):
    entry = make_entry(
        "misc", "t", role=role, pauses=pauses, needs_workspace=needs_ws, gate=gate,
    )
    page = reference_pages([entry], used_in={})["reference/tools/misc/t"]
    block = page.body.split("## Facts\n", 1)[1].split("\n## ", 1)[0]
    assert [line for line in block.splitlines() if line] == ["- Scoped id: misc__t", *facts]
    assert page.meta["role"] == role and page.meta["pauses_turn"] is pauses
    assert page.meta["needs_workspace"] is needs_ws
    assert page.meta["approval_by_default"] == ("required" if gate else "none")


def test_a_session_tool_page_shows_a_direct_call():
    entry = make_entry(
        "workspace", "read", needs_workspace=True,
        schema={"type": "object", "properties": {"path": {"type": "string"}}},
        examples=[ToolExample(args={"path": "README.md"})],
    )
    page = reference_pages([entry], used_in={})["reference/tools/workspace/read"]
    assert '"name": "workspace__read"' in page.body
    assert "Always on in a workspace session: call it directly. call_tool cannot reach it." in page.body
    assert "Call it through system__call_tool." not in page.body
    assert page.meta["call"] == "direct"
    assert _json_blocks(page.body)[0] == {"name": "workspace__read", "arguments": {"path": "README.md"}}


def test_a_call_tool_reference_page():
    """The call_tool page through the real catalog, not a stand-in.

    The stand-in derives the role fails-closed (``required_role or
    "admin"``); the door's own declaration is what the page must show.
    """
    import asyncio

    from primer.toolset.tool_catalog import collect_tool_entries
    from tests.docs._live_toolsets import built_in_providers

    entries = {
        e.scoped_id: e
        for e in asyncio.run(collect_tool_entries(built_in_providers()))
    }
    entry = entries["system__call_tool"]
    page = reference_pages([entry], used_in={})["reference/tools/system/call_tool"]
    assert page.kind == "reference" and page.title == "system__call_tool"
    assert page.meta["role"] == "user"
    assert page.meta["pauses_turn"] is True
    assert _headings(page.body) == SIX_HEADINGS
    assert "- Pauses the turn: yes" in page.body
    assert "Call it through system__call_tool." in page.body


def test_the_description_is_shown_without_its_rendered_example_lines():
    entry = make_entry(
        "misc", "t", purpose="Do it.", when="Use when it is needed.",
        examples=[ToolExample(args={}, returns="done"), ToolExample(args={"tz": "UTC"})],
    )
    assert "Example: {}" in entry.tool.description          # make_tool rendered them into the description
    page = reference_pages([entry], used_in={})["reference/tools/misc/t"]
    what = page.body.split("## What it does\n", 1)[1].split("\n## ", 1)[0]
    assert what.strip() == "Do it.\n\nUse when it is needed."
    assert "Example:" not in what
    assert page.body.count("`{\"tz\":\"UTC\"}`") == 1        # the example is listed once, under Examples


@pytest.mark.parametrize(
    "examples",
    [
        [ToolExample(args={})],
        [ToolExample(args={}, returns="done"), ToolExample(args={"tz": "UTC"}, note="a note")],
        [ToolExample(args={"tz": "UTC"}, returns="done", note="both")],
        [ToolExample(args={}, returns="line one\nline two")],
        [ToolExample(args={}, note="a note\nover two lines"), ToolExample(args={"tz": "UTC"}, returns="ok")],
    ],
    ids=["bare", "returns-and-note", "both-on-one", "multi-line-returns", "multi-line-note"],
)
def test_every_shape_of_rendered_example_comes_out_of_the_prose_and_is_listed_once(examples):
    entry = make_entry("misc", "t", purpose="Do it.", when="Use when it is needed.", examples=examples)
    page = reference_pages([entry], used_in={})["reference/tools/misc/t"]
    what = page.body.split("## What it does\n", 1)[1].split("\n## Call it", 1)[0]
    assert what.strip() == "Do it.\n\nUse when it is needed."
    listed = page.body.split("## Examples\n", 1)[1].split("\n## Used in", 1)[0].strip().splitlines()
    assert len(listed) == len(examples) and all(line.startswith("- `") for line in listed)


def test_a_description_that_is_not_made_by_make_tool_is_kept_whole():
    entry = _bare_entry("Plain text.\nExample: this line is part of the prose.")
    page = reference_pages([entry], used_in={})["reference/tools/misc/t"]
    assert "Example: this line is part of the prose." in page.body


def test_a_tool_without_a_description_or_examples_says_none():
    entry = _bare_entry("")
    page = reference_pages([entry], used_in={})["reference/tools/misc/t"]
    what = page.body.split("## What it does\n", 1)[1].split("\n## ", 1)[0]
    assert what.strip() == "_(no description)_"
    examples = page.body.split("## Examples\n", 1)[1].split("\n## ", 1)[0]
    assert examples.strip() == "_(none)_"
    assert page.summary == ""


def test_an_example_whose_text_holds_backticks_keeps_its_code_span():
    entry = make_entry(
        "misc", "t", schema={"type": "object", "properties": {"s": {"type": "string"}}},
        examples=[ToolExample(args={"s": "a`b"}), ToolExample(args={"s": "``"})],
    )
    page = reference_pages([entry], used_in={})["reference/tools/misc/t"]
    examples = page.body.split("## Examples\n", 1)[1].split("\n## ", 1)[0]
    assert "- ``{\"s\":\"a`b\"}``" in examples
    assert "- ```{\"s\":\"``\"}```" in examples


def test_the_arguments_block_is_the_sorted_schema():
    schema = {"type": "object", "properties": {"b": {"type": "string"}, "a": {"type": "integer"}}, "required": ["a"]}
    entry = make_entry("misc", "t", schema=schema, examples=[ToolExample(args={"a": 1})])
    page = reference_pages([entry], used_in={})["reference/tools/misc/t"]
    args_block = re.search(r"## Arguments\n\n```json\n(.*?)\n```", page.body, flags=re.S).group(1)
    assert args_block == json.dumps(schema, indent=2, sort_keys=True)
    assert list(json.loads(args_block)["properties"]) == ["a", "b"]


def test_non_ascii_text_is_written_as_text_not_as_escapes():
    entry = make_entry(
        "misc", "t", purpose=f"Caf{E_ACUTE} {DASH} a tool.",
        schema={"type": "object", "properties": {"s": {"type": "string", "description": f"d{ELLIPSIS}"}}},
        examples=[ToolExample(args={})],
    )
    page = reference_pages([entry], used_in={})["reference/tools/misc/t"]
    assert f"Caf{E_ACUTE} {DASH} a tool." in page.body and f"d{ELLIPSIS}" in page.body
    assert "\\u" not in page.body


def test_used_in_lists_each_feature_page_once_sorted():
    entry = make_entry("misc", "t")
    page = reference_pages(
        [entry], used_in={"reference/tools/misc/t": ["b/page", "a/page", "b/page"]},
    )["reference/tools/misc/t"]
    assert page.body.endswith("## Used in\n\n- [a/page](a/page)\n- [b/page](b/page)\n")


def test_a_tool_no_feature_page_links_says_so():
    page = reference_pages([make_entry("misc", "t")], used_in={"reference/tools/misc/other": ["a/page"]})[
        "reference/tools/misc/t"
    ]
    assert page.body.endswith("## Used in\n\n_(not yet documented in a feature page)_\n")


def test_rendering_does_not_touch_the_tool():
    entry = make_entry("misc", "t", examples=[ToolExample(args={"tz": "UTC"})])
    before = (entry.tool.model_dump_json(), json.dumps(entry.tool.args_schema), entry.tool.examples[0].args.copy())
    reference_pages([entry], used_in={})
    assert (entry.tool.model_dump_json(), json.dumps(entry.tool.args_schema), entry.tool.examples[0].args) == before


# ---- summaries --------------------------------------------------------------------


def _summary(description: str) -> str:
    return reference_pages([_bare_entry(description)], used_in={})["reference/tools/misc/t"].summary


def test_the_summary_is_the_first_line_of_the_description():
    assert _summary("Return the time.\n\nUse when you need it.\nExample: {}") == "Return the time."


def test_the_summary_skips_blank_lines_and_collapses_whitespace():
    assert _summary("\n\n  Return   the\ttime.  \nmore") == "Return the time."


def test_a_first_line_of_exactly_the_limit_is_kept_whole():
    line = "x" * SUMMARY_MAX
    assert _summary(line) == line


def test_a_too_long_first_line_is_cut_to_its_first_sentence():
    first = "Materialise a new Workspace from a template."
    assert _summary(f"{first} Looks up the template, asks the matching backend to create the live instance, "
                    "then persists a Workspace row with the assigned id, and returns the stored row.") == first


def test_a_first_sentence_that_fills_the_limit_is_kept_and_one_character_more_is_not():
    fits = "x" * (SUMMARY_MAX - 1) + "."
    assert len(fits) == SUMMARY_MAX
    assert _summary(f"{fits} And then more words that push the line past the limit.") == fits
    too_long = "x" * SUMMARY_MAX + "."
    got = _summary(f"{too_long} And then more words that push the line past the limit.")
    assert len(got) <= SUMMARY_MAX and got.endswith("...") and got != too_long


def test_a_too_long_first_line_without_a_short_sentence_is_cut_at_a_word_with_dots():
    words = "word " * 60
    got = _summary(words)
    assert got.endswith("...") and len(got) <= SUMMARY_MAX
    assert got[:-3] == ("word " * 60)[: len(got) - 3].rstrip()
    assert "  " not in got and not got[:-3].endswith(" ")


def test_one_unbroken_token_is_cut_at_the_limit():
    got = _summary("x" * 400)
    assert got == "x" * (SUMMARY_MAX - 3) + "..."


# ---- the index pages ---------------------------------------------------------------


def test_the_index_pages_list_their_children():
    entries = [
        make_entry("misc", "get_datetime", purpose="Return the time."),
        make_entry("misc", "echo", purpose="Echo the input."),
        make_entry("workspace", "read", needs_workspace=True, purpose="Read a file."),
    ]
    pages = reference_pages(entries, used_in={})
    assert sorted(pages) == [
        "reference",
        "reference/tools",
        "reference/tools/misc",
        "reference/tools/misc/echo",
        "reference/tools/misc/get_datetime",
        "reference/tools/workspace",
        "reference/tools/workspace/read",
    ]
    assert {p.kind for p in pages.values()} == {"reference"}
    assert all(p.meta["kind"] == "reference" and p.meta["summary"] == p.summary for p in pages.values())

    assert "\n## Pages\n\n- [Tools](reference/tools) - One page per tool, grouped by toolset.\n" in pages["reference"].body
    assert pages["reference"].title == "Reference"
    assert pages["reference"].summary == "every agent-callable tool, generated from the live registry"

    tools_lines = pages["reference/tools"].body.split("## Pages\n\n", 1)[1].splitlines()
    assert tools_lines == [
        "- [misc](reference/tools/misc) - 2 tools, called through system__call_tool",
        "- [workspace](reference/tools/workspace) - 1 tool, always on in a workspace session and called directly",
    ]
    misc_lines = pages["reference/tools/misc"].body.split("## Pages\n\n", 1)[1].splitlines()
    assert misc_lines == [
        "- [misc__echo](reference/tools/misc/echo) - Echo the input.",
        "- [misc__get_datetime](reference/tools/misc/get_datetime) - Return the time.",
    ]
    assert pages["reference/tools/misc"].meta["toolset"] == "misc"
    assert "Call them through system__call_tool." in pages["reference/tools/misc"].body
    assert "call_tool cannot reach them" in pages["reference/tools/workspace"].body


def test_no_tools_still_gives_the_two_roots():
    pages = reference_pages([], used_in={})
    assert sorted(pages) == ["reference", "reference/tools"]
    assert pages["reference/tools"].body.endswith("## Pages\n\n_(none)_\n")


def test_two_entries_that_claim_one_reference_path_raise():
    """A second descriptor for the same toolset and tool is a build error, not a warn-and-drop: the page it would
    render is ambiguous, and the boot pass keeps the stored wiki behind its WikiSourceError catch."""
    first = _generated_entry()
    second = make_entry("agents", "get_agent")
    with pytest.raises(WikiSourceError, match="reference/tools/agents/get_agent"):
        reference_pages([first, second], used_in={})


def test_removing_an_entry_removes_its_page_and_its_purpose_row():
    # the by-purpose builder reads the descriptor FIELD (make_tool's purpose/when are prose)
    a = entry_from_tool(Tool(
        id="get_datetime", toolset_id="misc", description="Return the current date and time.",
        args_schema={"type": "object"}, required_role="user", purpose="Return the current time.",
    ))
    b = entry_from_tool(Tool(
        id="echo", toolset_id="misc", description="Echo the input.",
        args_schema={"type": "object"}, required_role="user", purpose="Echo the input back.",
    ))
    # the rows sort by purpose, which here is the reverse of the entry order
    assert by_purpose_rows([a, b]) == [
        "| Echo the input back. | reference/tools/misc/echo |",
        "| Return the current time. | reference/tools/misc/get_datetime |",
    ]
    both = reference_pages([a, b], used_in={})
    assert {"reference/tools/misc/get_datetime", "reference/tools/misc/echo"} <= set(both)
    left = reference_pages([a], used_in={})
    assert "reference/tools/misc/echo" not in left
    assert by_purpose_rows([a]) == ["| Return the current time. | reference/tools/misc/get_datetime |"]
    assert by_purpose_rows([]) == []


# ---- generated metadata (M3) --------------------------------------------------------
#
# The six wire-excluded Tool fields carry the structured documentation the generated
# reference page renders; curated tools (make_tool) leave them None and render exactly
# as before (the GOLDEN and the meta pins above are the byte-identity guard).


GEN_ORIGIN = "GET /v1/agents/{agent_id}"
GEN_OPERATION = "get_agent"
GEN_RESULT = "the GET /v1/agents/{agent_id} response body (JSON parsed; non-JSON base64)"
GEN_ERRORS = "a 4xx/5xx answer is an error result carrying the route's body (RFC 7807 problem+json where the route maps errors)"


def _generated_entry() -> StandInEntry:
    tool = Tool(
        id="get_agent",
        toolset_id="agents",
        description="Fetch one Agent by id.",
        args_schema={"type": "object", "properties": {"agent_id": {"type": "string"}}, "required": ["agent_id"]},
        required_role="user",
        purpose="Look up one Agent by its id.",
        use_when="Use when you have an agent id and want its row.",
        origin=GEN_ORIGIN,
        operation=GEN_OPERATION,
        result_contract=GEN_RESULT,
        error_contract=GEN_ERRORS,
    )
    return entry_from_tool(tool)


def test_a_generated_entry_renders_its_metadata_lines_and_section():
    entry = _generated_entry()
    page = reference_pages([entry], used_in={})["reference/tools/agents/get_agent"]
    body = page.body
    what = body.split("## What it does\n", 1)[1].split("\n## ", 1)[0]
    assert what.strip() == (
        "Fetch one Agent by id.\n\n"
        "- Purpose: Look up one Agent by its id.\n"
        "- Use when: Use when you have an agent id and want its row."
    )
    # the section sits between Arguments and Facts
    section = body.split("## Result and errors\n", 1)[1].split("\n## ", 1)[0]
    assert section.strip() == f"- Result: {GEN_RESULT}\n- Errors: {GEN_ERRORS}"
    facts = body.split("## Facts\n", 1)[1].split("\n## ", 1)[0]
    assert f"- Origin: {GEN_ORIGIN}" in facts and f"- Operation: {GEN_OPERATION}" in facts
    assert _headings(body) == [
        "## What it does", "## Call it", "## Arguments", "## Result and errors",
        "## Facts", "## Examples", "## Used in",
    ]
    assert page.meta["origin"] == GEN_ORIGIN
    assert page.meta["operation"] == GEN_OPERATION
    assert page.meta["purpose"] == "Look up one Agent by its id."


def test_the_result_and_errors_section_is_omitted_when_a_contract_is_missing():
    entry = _generated_entry()
    missing_result = entry_from_tool(entry.tool.model_copy(update={"result_contract": None}))
    assert "## Result and errors" not in reference_pages([missing_result], used_in={})[
        "reference/tools/agents/get_agent"
    ].body
    # a curated tool (every field None) renders the six headings, no new lines
    curated = entry_from_tool(
        entry.tool.model_copy(
            update={name: None for name in ("purpose", "use_when", "origin", "operation", "result_contract", "error_contract")},
        )
    )
    page = reference_pages([curated], used_in={})["reference/tools/agents/get_agent"]
    assert _headings(page.body) == SIX_HEADINGS
    assert "Purpose:" not in page.body and "Origin:" not in page.body
    assert "origin" not in page.meta and "operation" not in page.meta and "purpose" not in page.meta


def test_pages_come_back_sorted_and_the_same_every_time():
    entries = [make_entry("zeta", "b"), make_entry("alpha", "z"), make_entry("alpha", "a")]
    first = reference_pages(entries, used_in={})
    assert list(first) == sorted(first)
    assert first == reference_pages(list(reversed(entries)), used_in={})


# ---- the production shape ------------------------------------------------------------


def test_every_tool_of_the_built_in_toolsets_renders_a_well_formed_page():
    entries = real_entries()
    assert len(entries) > 150
    before = {e.scoped_id: json.dumps(e.tool.args_schema, sort_keys=True) for e in entries}
    pages = reference_pages(entries, used_in={})
    tool_pages = {p: v for p, v in pages.items() if p.count("/") == 3}
    assert len(tool_pages) == len(entries)
    for entry in entries:
        page = pages[reference_path(entry.toolset_id, entry.tool.id)]
        context = entry.scoped_id
        assert _headings(page.body) == SIX_HEADINGS, context
        assert page.body.startswith(f"# {entry.scoped_id}\n\n## What it does\n"), context
        assert "\n" not in page.summary and 0 < len(page.summary) <= SUMMARY_MAX, context
        blocks = _json_blocks(page.body)
        assert len(blocks) == 2, context
        assert blocks[0] == call_example(entry), context
        assert blocks[1] == entry.tool.args_schema, context
        what = page.body.split("## What it does\n", 1)[1].split("\n## Call it", 1)[0]
        assert not [ln for ln in what.splitlines() if ln.startswith(("Example:", "#"))], context
        assert all(ln.startswith(("- ", "_(")) or not ln for ln in page.body.split("## Examples\n", 1)[1]
                   .split("\n## Used in", 1)[0].splitlines()), context
    assert {e.scoped_id: json.dumps(e.tool.args_schema, sort_keys=True) for e in entries} == before
