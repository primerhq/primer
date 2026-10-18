"""A workspace's agents.md: the loader, the Jinja escape and the prompt part (pivot P2, Task 7).

The platform reads ``agents.md`` (falling back to ``AGENTS.md``) from the workspace root every turn and appends
it to the system prompt of every agent that runs there. The prompt is a Jinja template, so the file text must
reach the model literally: a user's ``{{ ... }}`` or ``{% ... %}`` is text, never a template. The file is capped
at 32 KiB, a missing file is normal, and an unreadable one is logged once and skipped (the turn still runs).
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from primer.agent.prompt_render import render_system_prompt
from primer.model.agent import Agent, AgentModel
from primer.model.except_ import BadRequestError, NotFoundError
from primer.model.graph import build_execution_context
from primer.workspace.instructions import (
    AGENTS_MD_MAX_BYTES,
    AGENTS_MD_NAMES,
    WORKSPACE_INSTRUCTIONS_TITLE,
    jinja_literal,
    load_workspace_instructions,
    workspace_instructions_part,
    workspace_instructions_parts,
)


class _Ws:
    """A workspace whose ``read_file`` returns the stored bytes, raises the stored exception, or raises
    ``NotFoundError`` for a path it does not hold (the contract of ``Workspace.read_file``)."""

    id = "ws-1"

    def __init__(self, files: dict[str, bytes | BaseException]) -> None:
        self._files = files
        self.reads: list[str] = []

    async def read_file(self, path: str) -> bytes:
        self.reads.append(path)
        if path not in self._files:
            raise NotFoundError(f"{path!r} not found")
        value = self._files[path]
        if isinstance(value, BaseException):
            raise value
        return value


def _agent() -> Agent:
    return Agent(id="a-1", description="an agent", model=AgentModel(profile_id="p--m"))


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# Which file, and what a missing or unreadable one does
# ---------------------------------------------------------------------------


def test_the_constants_are_the_ones_the_design_names() -> None:
    assert AGENTS_MD_NAMES == ("agents.md", "AGENTS.md")
    assert AGENTS_MD_MAX_BYTES == 32768
    assert WORKSPACE_INSTRUCTIONS_TITLE == "Workspace instructions (agents.md)"


async def test_reads_agents_md():
    assert await load_workspace_instructions(_Ws({"agents.md": b"Be brief."})) == "Be brief."


async def test_falls_back_to_upper_case_only_when_lower_case_is_absent():
    assert await load_workspace_instructions(_Ws({"AGENTS.md": b"UP"})) == "UP"
    assert await load_workspace_instructions(_Ws({"agents.md": b"low", "AGENTS.md": b"UP"})) == "low"


async def test_the_lower_case_file_is_read_first_and_the_other_only_when_it_is_absent():
    both = _Ws({"agents.md": b"low", "AGENTS.md": b"UP"})
    await load_workspace_instructions(both)
    assert both.reads == ["agents.md"]
    only_upper = _Ws({"AGENTS.md": b"UP"})
    await load_workspace_instructions(only_upper)
    assert only_upper.reads == ["agents.md", "AGENTS.md"]


async def test_a_missing_file_is_none_and_logs_nothing(caplog):
    caplog.set_level(logging.DEBUG)
    assert await load_workspace_instructions(_Ws({})) is None
    assert _warnings(caplog) == []


async def test_a_directory_is_skipped_with_one_warning(caplog):
    ws = _Ws({"agents.md": BadRequestError("'agents.md' is not a file"), "AGENTS.md": b"UP"})
    assert await load_workspace_instructions(ws) is None
    assert len(_warnings(caplog)) == 1
    assert ws.reads == ["agents.md"], "an unreadable agents.md is skipped, not replaced by AGENTS.md"


async def test_the_warning_names_the_file_the_workspace_and_the_error(caplog):
    await load_workspace_instructions(_Ws({"agents.md": BadRequestError("'agents.md' is not a file")}))
    (record,) = _warnings(caplog)
    assert record.getMessage() == (
        "workspace instructions: agents.md in ws-1 is unreadable; skipped: 'agents.md' is not a file"
    )


@pytest.mark.parametrize("error", [OSError("disk gone"), RuntimeError("sandbox closed"), PermissionError("denied")])
async def test_any_read_error_is_skipped_with_one_warning(caplog, error):
    assert await load_workspace_instructions(_Ws({"agents.md": error})) is None
    assert len(_warnings(caplog)) == 1


async def test_an_unreadable_upper_case_file_is_skipped_with_one_warning_too(caplog):
    assert await load_workspace_instructions(_Ws({"AGENTS.md": OSError("denied")})) is None
    (record,) = _warnings(caplog)
    assert "AGENTS.md" in record.getMessage()


async def test_a_workspace_without_an_id_still_warns_instead_of_raising(caplog):
    class _NoId:
        async def read_file(self, path: str) -> bytes:
            raise OSError("denied")

    assert await load_workspace_instructions(_NoId()) is None
    assert len(_warnings(caplog)) == 1


async def test_a_cancellation_is_not_swallowed():
    """The loader runs inside a turn's build; a Stop that cancels it must reach the caller, not be logged away."""
    ws = _Ws({"agents.md": asyncio.CancelledError()})
    with pytest.raises(asyncio.CancelledError):
        await load_workspace_instructions(ws)


async def test_an_empty_file_still_shadows_the_upper_case_one():
    assert await load_workspace_instructions(_Ws({"agents.md": b"", "AGENTS.md": b"UP"})) == ""


# ---------------------------------------------------------------------------
# Decoding and the 32 KiB cap
# ---------------------------------------------------------------------------


async def test_non_utf8_bytes_are_replaced():
    assert await load_workspace_instructions(_Ws({"agents.md": b"caf\xe9"})) == "caf�"


async def test_the_cap_truncates_with_the_note():
    text = await load_workspace_instructions(_Ws({"agents.md": b"a" * 40000}))
    assert text == "a" * 32768 + "\n\n[truncated: agents.md is 40000 bytes; only the first 32768 bytes are included]"


async def test_a_file_of_exactly_the_cap_is_whole_and_one_byte_more_is_cut():
    whole = await load_workspace_instructions(_Ws({"agents.md": b"a" * AGENTS_MD_MAX_BYTES}))
    assert whole == "a" * AGENTS_MD_MAX_BYTES
    cut = await load_workspace_instructions(_Ws({"agents.md": b"a" * (AGENTS_MD_MAX_BYTES + 1)}))
    assert cut.startswith("a" * AGENTS_MD_MAX_BYTES + "\n\n[truncated: agents.md is 32769 bytes")


async def test_the_note_names_the_file_that_was_cut():
    text = await load_workspace_instructions(_Ws({"AGENTS.md": b"b" * 50000}))
    assert text.endswith("[truncated: AGENTS.md is 50000 bytes; only the first 32768 bytes are included]")


async def test_the_cap_counts_bytes_not_characters():
    # 20000 characters of 3 bytes each is 60000 bytes: over the cap although far under it in characters.
    text = await load_workspace_instructions(_Ws({"agents.md": ("€" * 20000).encode()}))
    assert text.endswith("[truncated: agents.md is 60000 bytes; only the first 32768 bytes are included]")


async def test_a_character_cut_in_half_by_the_cap_is_dropped_not_replaced():
    # 32768 = 3 * 10922 + 2: the cut lands inside the 10923rd euro sign.
    text = await load_workspace_instructions(_Ws({"agents.md": ("€" * 20000).encode()}))
    body = text.split("\n\n[truncated:")[0]
    assert body == "€" * 10922
    assert "�" not in body


async def test_bad_bytes_before_the_cut_are_replaced_as_they_are_under_the_cap():
    raw = b"caf\xe9 " + b"a" * 40000
    text = await load_workspace_instructions(_Ws({"agents.md": raw}))
    assert text.startswith("caf� aaa")


# ---------------------------------------------------------------------------
# The Jinja escape and the prompt part
# ---------------------------------------------------------------------------


def test_jinja_syntax_reaches_the_model_literally():
    raw = '{{ ctx.workspace_id }} {% raw %}x{% endraw %} {# c #} {% if true %}y{% endif %} {{ "{'
    rendered = render_system_prompt(["intro", workspace_instructions_part(raw)], build_execution_context())
    assert rendered == "intro\n\n## Workspace instructions (agents.md)\n\n" + raw


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "{{",
        "}}",
        "{{{",
        "{}",
        "{}}",
        "a{}b",
        "{{ }}",
        "{{{{ x }}}}",
        "{%",
        "{%- if x -%} y {%- endif -%}",
        "{#",
        "{# unterminated",
        "{% raw %}",
        "{{ undefined_name }}",
        "{{ 7 * 6 }}",
        "{{ ''.__class__ }}",
        '{{ "{" }}',
        '{{ "{" }}{{ "{" }}',
        "```json\n{\"a\": {\"b\": [1, 2]}}\n```",
        "function f() { return {}; }",
        "line one\n{{ trailing }}\n",
        "\n\n  leading and trailing blanks  \n\n",
        "",
    ],
)
def test_no_file_text_is_ever_a_template(raw):
    rendered = render_system_prompt([workspace_instructions_part(raw)], build_execution_context())
    assert rendered == "## Workspace instructions (agents.md)\n\n" + raw


def test_jinja_literal_changes_only_the_opening_brace():
    assert jinja_literal("no braces here") == "no braces here"
    assert jinja_literal("{") == '{{ "{" }}'
    assert jinja_literal("a } b") == "a } b"


def test_the_part_is_a_heading_a_blank_line_and_the_escaped_text():
    assert workspace_instructions_part("Be brief.") == "## Workspace instructions (agents.md)\n\nBe brief."
    assert workspace_instructions_part("{x}") == '## Workspace instructions (agents.md)\n\n{{ "{" }}x}'


def test_the_escape_does_not_reach_the_rest_of_the_prompt():
    """Only the file text is a literal: the agent's own prompt keeps its Jinja, as it always has."""
    ctx = build_execution_context(surface="workspace", workspace_id="ws-9", session_id="s-1")
    rendered = render_system_prompt(["id={{ ctx.workspace_id }}", workspace_instructions_part("{{ ctx.workspace_id }}")], ctx)
    assert rendered == "id=ws-9\n\n## Workspace instructions (agents.md)\n\n{{ ctx.workspace_id }}"


# ---------------------------------------------------------------------------
# The one function every prompt site calls
# ---------------------------------------------------------------------------


def test_no_text_no_part():
    assert workspace_instructions_parts(_agent(), None) == []


def test_text_is_one_part():
    assert workspace_instructions_parts(_agent(), "rules") == [workspace_instructions_part("rules")]


@pytest.mark.parametrize("blank", ["", "   ", "\n\n", " \t\r\n"])
def test_a_blank_file_adds_no_part(blank):
    """A heading with nothing under it would only cost tokens and read as an empty instruction."""
    assert workspace_instructions_parts(_agent(), blank) == []
