"""Reference pages: one generated page per agent-callable tool.

``reference_pages`` turns a list of tool entries (the tool catalog of pivot P1 task 5)
into ``reference/tools/<toolset>/<tool>`` pages plus the index pages above them, so a
page cannot drift from the code: it is rendered from the tool's own descriptor.

The entries are read through :class:`ToolEntryView`, which is exactly the part of
``primer.toolset.tool_catalog.ToolEntry`` the pages use. Reading them through a protocol
keeps ``primer.knowledge`` from importing ``primer.toolset`` (the two packages import each
other's modules).
"""
from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from primer.knowledge.wiki.pages import SUMMARY_MAX, WikiPage, page_line, sibling_key
from primer.model.chat import Tool

logger = logging.getLogger(__name__)

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

    The arguments are the tool's first example, else ``{}``.
    """
    examples = entry.tool.examples
    arguments = copy.deepcopy(examples[0].args) if examples else {}
    if entry.via_call_tool:
        return {"toolset_id": entry.toolset_id, "tool_name": entry.tool.id, "arguments": arguments}
    return {"name": entry.scoped_id, "arguments": arguments}


_SENTENCE_RE = re.compile(rf"(.{{20,{SUMMARY_MAX}}}?[.!?])(?:\s|$)")


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


def _prose(tool: Tool) -> str:
    """The description without the ``Example:`` lines ``make_tool`` rendered into it.

    They are listed under ``## Examples``, so showing them twice would only cost tokens.
    """
    lines = tool.description.rstrip().split("\n")
    count = len(tool.examples)
    if count and len(lines) >= count and all(line.startswith("Example: ") for line in lines[-count:]):
        lines = lines[:-count]
    return "\n".join(lines).rstrip() or "_(no description)_"


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
    body = "\n".join([
        f"# {entry.scoped_id}",
        "",
        "## What it does",
        "",
        _prose(tool),
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
        "",
        "## Facts",
        "",
        f"- Scoped id: {entry.scoped_id}",
        f"- Role: {entry.required_role}",
        f"- Approval by default: {approval}",
        f"- Pauses the turn: {_yes_no(entry.pauses_turn)}",
        f"- Needs a workspace: {_yes_no(entry.needs_workspace)}",
        "",
        "## Examples",
        "",
        *_example_lines(tool),
        "",
        "## Used in",
        "",
        *used_in,
    ]) + "\n"
    return WikiPage(
        path=reference_path(entry.toolset_id, tool.id),
        title=entry.scoped_id,
        summary=summary,
        body=body,
        kind="reference",
        meta={
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
        },
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
    same toolset and tool is logged and dropped.
    """
    tool_pages: dict[str, WikiPage] = {}
    by_toolset: dict[str, list[WikiPage]] = {}
    via_call_tool: dict[str, bool] = {}
    for entry in sorted(entries, key=lambda e: (e.toolset_id, e.tool.id)):
        path = reference_path(entry.toolset_id, entry.tool.id)
        if path in tool_pages:
            logger.warning("wiki reference: two tools claim %s; keeping the first", path)
            continue
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
