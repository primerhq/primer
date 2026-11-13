"""Reference pages: one generated page per agent-callable tool.

``reference_pages`` turns a list of tool entries (the tool catalog of pivot P1 task 5)
into ``reference/tools/<toolset>/<tool>`` pages plus the index pages above them, so a
page cannot drift from the code: it is rendered from the tool's own descriptor.

The generated REST-backed tools carry structured documentation on the descriptor itself
(M3: the wire-excluded ``purpose``/``use_when``/``origin``/``operation``/``result_contract``/
``error_contract`` fields, set only by ``route_tools._tool_from_blueprint``). Their pages
render the ``Purpose:``/``Use when:`` lines under "What it does", a ``Result and errors``
section, and the ``Origin:``/``Operation:`` facts. A curated tool keeps every field None
and renders byte-identically to the plain six-section page. Two entries claiming one path
raise :class:`~primer.knowledge.wiki.pages.WikiSourceError`.

The entries are read through :class:`ToolEntryView`, which is exactly the part of
``primer.toolset.tool_catalog.ToolEntry`` the pages use. Reading them through a protocol
keeps ``primer.knowledge`` from importing ``primer.toolset`` (the two packages import each
other's modules).
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from primer.knowledge.wiki.pages import SUMMARY_MAX, WikiPage, WikiSourceError, page_line, sibling_key
from primer.model.chat import Tool, ToolExample

REFERENCE_ROOT = "reference"
REFERENCE_TOOLS = "reference/tools"

REFERENCE_SUMMARY = "every agent-callable tool, generated from the live registry"
"""The root reference page's summary; the table of contents shows the same words."""

CALL_TOOL = "system__call_tool"

_CALL_VIA_CALL_TOOL = f"Call it through {CALL_TOOL}."
_CALL_DIRECT = "Always on in a workspace session: call it directly. call_tool cannot reach it."
_NOT_DOCUMENTED = "_(not yet documented in a feature page)_"
_NONE = "_(none)_"


class ToolEntryView(Protocol):
    """What a reference page reads of a tool entry (the fields of ``ToolEntry``)."""

    @property
    def toolset_id(self) -> str: ...

    @property
    def tool(self) -> Tool: ...

    @property
    def required_role(self) -> str: ...

    @property
    def pauses_turn(self) -> bool: ...

    @property
    def needs_workspace(self) -> bool: ...

    @property
    def default_gate(self) -> str | None: ...

    @property
    def scoped_id(self) -> str: ...

    @property
    def via_call_tool(self) -> bool: ...


def reference_path(toolset_id: str, tool_name: str) -> str:
    return f"{REFERENCE_TOOLS}/{toolset_id}/{tool_name}"


def call_example(entry: ToolEntryView) -> dict[str, Any]:
    """A call an agent can send as it stands: through ``call_tool``, or direct for a session tool.

    The arguments are the tool's first example; a tool with no examples (the generated route tools
    carry none today) gets the minimal arguments its argument schema requires, so the example always
    validates against the tool's own ``args_schema`` (the docs coverage test checks every example on
    every page against the live schemas).
    """
    examples = entry.tool.examples
    arguments = copy.deepcopy(examples[0].args) if examples else _minimal_arguments(entry.tool.args_schema)
    if entry.via_call_tool:
        return {"toolset_id": entry.toolset_id, "tool_name": entry.tool.id, "arguments": arguments}
    return {"name": entry.scoped_id, "arguments": arguments}


def _resolve_ref(ref: str, root: dict[str, Any]) -> dict[str, Any] | None:
    """The node a JSON-pointer ``$ref`` names inside ``root`` (``#/$defs/<name>`` and friends)."""
    node: Any = root
    for part in ref.lstrip("#").strip("/").split("/"):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, dict) else None


def _passes(value: Any, branch: dict[str, Any], root: dict[str, Any]) -> bool:
    """Whether ``value`` validates against ``branch``, with the root's ``$defs`` in scope for the branch's refs."""
    import jsonschema

    defs = root.get("$defs", root.get("definitions", {}))
    if isinstance(branch.get("$defs"), dict):
        defs = {**defs, **branch["$defs"]}
    schema = {**branch}
    if defs:
        schema["$defs"] = defs
    try:
        jsonschema.validate(value, schema)
        return True
    except jsonschema.ValidationError:
        return False


def _placeholder(prop: Any, root: dict[str, Any], seen: frozenset[str] = frozenset()) -> Any:
    """A value that validates against the property schema ``prop``.

    A ``$ref`` resolves inside ``root``; a composition takes the first branch whose generated value
    passes its own branch (and, for ``oneOf``, no other branch); a required sub-property of an
    object gets its own placeholder. A cyclic ``$ref`` yields ``None`` (JSON null), which the
    generated schemas accept nowhere on a required property today.
    """
    if not isinstance(prop, dict):
        return None
    if "$ref" in prop:
        if prop["$ref"] in seen:
            return None
        node = _resolve_ref(prop["$ref"], root)
        if node is None:
            return None
        return _placeholder(node, root, seen | {prop["$ref"]})
    if "const" in prop:
        return prop["const"]
    if "enum" in prop:
        return prop["enum"][0] if prop["enum"] else None
    if "oneOf" in prop or "anyOf" in prop:
        branches = [branch for branch in (prop.get("oneOf") or prop.get("anyOf") or ()) if isinstance(branch, dict)]
        disc = prop.get("discriminator")
        disc_name = disc.get("propertyName") if isinstance(disc, dict) else None
        for branch in branches:
            candidate = _placeholder(branch, root, seen)
            # A discriminated oneOf (pydantic unions): name the branch in the discriminator field,
            # whose literal is a const/enum the placeholder takes as-is.
            branch_node = _resolve_ref(branch["$ref"], root) if "$ref" in branch else branch
            if (
                isinstance(candidate, dict)
                and isinstance(disc_name, str)
                and disc_name not in candidate
                and isinstance(branch_node, dict)
                and disc_name in (branch_node.get("properties") or {})
            ):
                disc_value = _placeholder(branch_node["properties"][disc_name], root, seen)
                if disc_value is not None:
                    candidate = {**candidate, disc_name: disc_value}
            if candidate is None or not _passes(candidate, branch, root):
                continue
            if "oneOf" in prop and any(_passes(candidate, other, root) for other in branches if other is not branch):
                continue
            return candidate
        return None
    if "allOf" in prop:
        merged: dict[str, Any] = {}
        for branch in prop["allOf"]:
            if isinstance(branch, dict):
                merged.update(branch)
        return _placeholder(merged, root, seen)
    if "type" not in prop:
        if "properties" not in prop:
            return None
        prop = {**prop, "type": "object"}
    typ = prop["type"] if isinstance(prop["type"], str) else next(iter(prop["type"]), None)
    if typ == "object":
        required = prop.get("required", ())
        properties = prop.get("properties") or {}
        return {key: _placeholder(properties.get(key), root, seen) for key in required}
    if typ == "array":
        min_items = int(prop.get("minItems", 0))
        items = prop.get("items")
        if min_items and isinstance(items, dict):
            return [_placeholder(items, root, seen) for _ in range(min_items)]
        return []
    if typ == "string":
        min_length = int(prop.get("minLength", 0))
        pattern = prop.get("pattern")
        if isinstance(pattern, str):
            derived = _pattern_value(pattern, min_length)
            if derived is not None:
                return derived
        return "x" * max(min_length, 1) if min_length else ""
    if typ in ("integer", "number"):
        minimum = 0
        for bound in (prop.get("minimum"), prop.get("exclusiveMinimum")):
            if isinstance(bound, (int, float)):
                minimum = max(minimum, bound)
        value = int(minimum) if typ == "integer" else minimum
        # shortcut: no generated schema pairs exclusiveMinimum with a value that is its own floor; re-pin if one lands
        return value + 1 if isinstance(prop.get("exclusiveMinimum"), (int, float)) else value
    if typ == "boolean":
        return False
    return None


def _first_of_class(class_body: str) -> str | None:
    """The first character a ``[...]`` class admits (a range starts with its lower bound)."""
    if class_body.startswith("\\"):
        return class_body[1] if len(class_body) > 1 else None
    return class_body[0] or None


def _pattern_value(pattern: str, min_length: int) -> str | None:
    """A value for a string constrained to one character-class repetition (the shape of ``^[0-9a-fA-F]{7,64}$``).

    shortcut: only that shape is read (the one the generated schemas use today); a pattern the helper does not
    read falls back to the plain string placeholder and the coverage test re-pins the gap.
    """
    body = pattern.strip("^$")
    match = re.fullmatch(r"(\[[^\]]+\])\{(\d+)(?:,\d+)?\}", body)
    if match is None:
        return None
    char = _first_of_class(match.group(1)[1:-1])
    if char is None:
        return None
    return char * max(int(match.group(2)), min_length, 1)


def _minimal_arguments(schema: dict[str, Any]) -> dict[str, Any]:
    """The minimal arguments object that validates against ``schema``: a typed placeholder per required property."""
    properties = schema.get("properties") or {}
    return {key: _placeholder(properties.get(key), schema) for key in schema.get("required", ())}


_SENTENCE_RE = re.compile(rf"(.{{20,{SUMMARY_MAX - 1}}}?[.!?])(?:\s|$)")


def _summary_of(description: str) -> str:
    """The first line of the description, shortened to the summary limit when it is longer.

    A too-long line is cut to its first sentence when that fits; otherwise at a word, with
    ``...`` at the end.
    """
    first = next((line for line in description.splitlines() if line.strip()), "")
    line = " ".join(first.split())
    if len(line) <= SUMMARY_MAX:
        return line
    sentence = _SENTENCE_RE.match(line)
    if sentence:
        return sentence.group(1)
    return line[: SUMMARY_MAX - 3].rsplit(" ", 1)[0].rstrip(" ,;:-") + "..."


def _rendered_example(example: ToolExample) -> str:
    """The line ``render_description`` (``primer/toolset/_describe.py``) writes into a description for one example.

    ``tests/knowledge/wiki/test_reference.py`` fails when the two drift apart: the examples would then
    show twice on a page, which costs tokens but is not wrong.
    """
    line = f"Example: {_compact(example.args)}"
    if example.returns:
        line += f" -> {example.returns}"
    if example.note:
        line += f"  ({example.note})"
    return line


def _prose(tool: Tool) -> str:
    """The description without the ``Example:`` lines ``make_tool`` rendered into it.

    They are listed under ``## Examples``, so showing them twice would only cost tokens. Only the
    exact rendering of the tool's own examples, at the very end, is removed.
    """
    description = tool.description
    if tool.examples:
        rendered = "\n" + "\n".join(_rendered_example(example) for example in tool.examples)
        if description.endswith(rendered):
            description = description[: -len(rendered)]
    return description.strip() or "_(no description)_"


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _code_span(text: str) -> str:
    """``text`` as an inline code span whose delimiter is longer than any backtick run inside it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    return f"{fence}{text}{fence}"


def _json_block(value: Any, *, sort_keys: bool) -> str:
    return "```json\n" + json.dumps(value, indent=2, sort_keys=sort_keys, ensure_ascii=False) + "\n```"


def _example_lines(tool: Tool) -> list[str]:
    lines = []
    for example in tool.examples:
        line = f"- {_code_span(_compact(example.args))}"
        if example.returns:
            line += f" -> {' '.join(example.returns.split())}"
        if example.note:
            line += f" ({' '.join(example.note.split())})"
        lines.append(line)
    return lines or [_NONE]


def _yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def _tool_page(entry: ToolEntryView, linked_from: Sequence[str]) -> WikiPage:
    tool = entry.tool
    approval = entry.default_gate or "none"
    summary = _summary_of(tool.description)
    used_in = [f"- [{path}]({path})" for path in sorted(set(linked_from))] or [_NOT_DOCUMENTED]
    body_parts: list[str] = [
        f"# {entry.scoped_id}",
        "",
        "## What it does",
        "",
        _prose(tool),
    ]
    # The structured metadata the generated tools carry (M3); a curated tool keeps every
    # field None and renders exactly the six sections it always had.
    metadata_lines: list[str] = []
    if tool.purpose is not None:
        metadata_lines.append(f"- Purpose: {tool.purpose}")
    if tool.use_when is not None:
        metadata_lines.append(f"- Use when: {tool.use_when}")
    if metadata_lines:
        body_parts += ["", *metadata_lines]
    body_parts += [
        "",
        "## Call it",
        "",
        _json_block(call_example(entry), sort_keys=False),
        "",
        _CALL_VIA_CALL_TOOL if entry.via_call_tool else _CALL_DIRECT,
        "",
        "## Arguments",
        "",
        _json_block(tool.args_schema, sort_keys=True),
    ]
    if tool.origin is not None and tool.result_contract is not None and tool.error_contract is not None:
        body_parts += [
            "",
            "## Result and errors",
            "",
            f"- Result: {tool.result_contract}",
            f"- Errors: {tool.error_contract}",
        ]
    body_parts += [
        "",
        "## Facts",
        "",
        f"- Scoped id: {entry.scoped_id}",
        f"- Role: {entry.required_role}",
        f"- Approval by default: {approval}",
        f"- Pauses the turn: {_yes_no(entry.pauses_turn)}",
        f"- Needs a workspace: {_yes_no(entry.needs_workspace)}",
    ]
    if tool.origin is not None:
        body_parts.append(f"- Origin: {tool.origin}")
    if tool.operation is not None:
        body_parts.append(f"- Operation: {tool.operation}")
    body_parts += [
        "",
        "## Examples",
        "",
        *_example_lines(tool),
        "",
        "## Used in",
        "",
        *used_in,
    ]
    body = "\n".join(body_parts) + "\n"
    meta: dict[str, Any] = {
        "kind": "reference",
        "summary": summary,
        "toolset": entry.toolset_id,
        "tool": tool.id,
        "scoped_id": entry.scoped_id,
        "role": entry.required_role,
        "pauses_turn": entry.pauses_turn,
        "needs_workspace": entry.needs_workspace,
        "approval_by_default": approval,
        "call": "call_tool" if entry.via_call_tool else "direct",
    }
    if tool.origin is not None:
        meta["origin"] = tool.origin
    if tool.operation is not None:
        meta["operation"] = tool.operation
    if tool.purpose is not None:
        meta["purpose"] = tool.purpose
    return WikiPage(
        path=reference_path(entry.toolset_id, tool.id),
        title=entry.scoped_id,
        summary=summary,
        body=body,
        kind="reference",
        meta=meta,
    )


def _index_page(
    path: str, title: str, summary: str, intro: str, children: Sequence[WikiPage], **meta: Any,
) -> WikiPage:
    listing = [page_line(child) for child in sorted(children, key=sibling_key)] or [_NONE]
    body = "\n".join([f"# {title}", "", intro, "", "## Pages", "", *listing]) + "\n"
    return WikiPage(
        path=path, title=title, summary=summary, body=body, kind="reference",
        meta={"kind": "reference", "summary": summary, **meta},
    )


def _toolset_page(toolset_id: str, via_call_tool: bool, tools: Sequence[WikiPage]) -> WikiPage:
    noun = "tool" if len(tools) == 1 else "tools"
    if via_call_tool:
        summary = f"{len(tools)} {noun}, called through {CALL_TOOL}"
        intro = f"Every tool of the {toolset_id} toolset. Call them through {CALL_TOOL}."
    else:
        summary = f"{len(tools)} {noun}, always on in a workspace session and called directly"
        intro = (
            f"Every tool of the {toolset_id} scope. They are always on in a workspace session: "
            "call them directly. call_tool cannot reach them."
        )
    return _index_page(
        f"{REFERENCE_TOOLS}/{toolset_id}", toolset_id, summary, intro, tools, toolset=toolset_id,
    )


def reference_pages(
    entries: Sequence[ToolEntryView], *, used_in: Mapping[str, Sequence[str]],
) -> dict[str, WikiPage]:
    """One page per entry, one index page per toolset, and the roots ``reference`` and ``reference/tools``.

    ``used_in`` maps a tool page's path to the feature pages that link to it. The roots exist
    even when there are no entries, so a link to them always resolves. A second entry for the
    same toolset and tool raises :class:`WikiSourceError` (a page two descriptors would render
    differently is a build error; the boot pass logs it and keeps the stored wiki).
    """
    tool_pages: dict[str, WikiPage] = {}
    by_toolset: dict[str, list[WikiPage]] = {}
    via_call_tool: dict[str, bool] = {}
    for entry in sorted(entries, key=lambda e: (e.toolset_id, e.tool.id)):
        path = reference_path(entry.toolset_id, entry.tool.id)
        if path in tool_pages:
            first = tool_pages[path]
            raise WikiSourceError(
                f"{path}: two entries claim the same scoped id {entry.scoped_id!r} "
                f"({first.meta['toolset']}::{first.meta['tool']} and {entry.toolset_id}::{entry.tool.id}); "
                "the reference page would be ambiguous"
            )
        page = _tool_page(entry, used_in.get(path, ()))
        tool_pages[path] = page
        by_toolset.setdefault(entry.toolset_id, []).append(page)
        via_call_tool[entry.toolset_id] = entry.via_call_tool

    toolset_pages = [
        _toolset_page(toolset_id, via_call_tool[toolset_id], by_toolset[toolset_id])
        for toolset_id in sorted(by_toolset)
    ]
    tools_page = _index_page(
        REFERENCE_TOOLS, "Tools", "One page per tool, grouped by toolset.",
        "Tools are grouped by toolset. Open a toolset to see its tools, or go straight to a tool's "
        "page when you know its toolset and name.",
        toolset_pages,
    )
    root_page = _index_page(
        REFERENCE_ROOT, "Reference", REFERENCE_SUMMARY,
        "One page per agent-callable tool, generated from the live tool registry when the platform "
        "starts, so a page cannot drift from the code. A page gives the tool's scoped id, a call you "
        "can send as it stands, its arguments schema, its role and whether it pauses the turn.",
        [tools_page],
    )
    pages = {page.path: page for page in [root_page, tools_page, *toolset_pages, *tool_pages.values()]}
    return dict(sorted(pages.items()))


__all__ = [
    "CALL_TOOL",
    "REFERENCE_ROOT",
    "REFERENCE_SUMMARY",
    "REFERENCE_TOOLS",
    "ToolEntryView",
    "call_example",
    "reference_pages",
    "reference_path",
]
