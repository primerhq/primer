"""Tests for primer.agent.prompt_render.render_system_prompt."""

from __future__ import annotations

import pytest

from primer.agent.prompt_render import render_system_prompt
from primer.model.except_ import BadRequestError
from primer.model.graph import build_execution_context

_BLOCK = (
    "You are helpful.\n\n"
    "{% if ctx.surface == 'workspace' %}"
    "Persist work under {{ ctx.artifact_dir }} and reply with file refs. "
    "Use inform_user and ask_user."
    "{% endif %}"
)


def test_workspace_surface_renders_block() -> None:
    ctx = build_execution_context(surface="workspace", session_id="sess-1")
    out = render_system_prompt([_BLOCK], ctx)
    assert "Persist work under artifacts/sess-1 and reply with file refs." in out
    assert "inform_user and ask_user" in out


def test_chat_surface_omits_block() -> None:
    ctx = build_execution_context(surface="chat")
    out = render_system_prompt([_BLOCK], ctx)
    assert "You are helpful." in out
    assert "Persist work under" not in out
    assert "inform_user" not in out


def test_join_semantics_preserved_for_plain_prompt() -> None:
    ctx = build_execution_context(surface="chat")
    out = render_system_prompt(["one", "two"], ctx)
    assert out == "one\n\ntwo"


def test_null_field_renders_none_not_raises() -> None:
    ctx = build_execution_context(surface="chat")  # artifact_dir is None
    out = render_system_prompt(["dir={{ ctx.artifact_dir }}"], ctx)
    assert out == "dir=None"


def test_typo_field_raises_bad_request() -> None:
    ctx = build_execution_context(surface="workspace", session_id="s")
    with pytest.raises(BadRequestError):
        render_system_prompt(["{{ ctx.surfce }}"], ctx)  # typo: surfce


def test_raw_block_passes_literal_braces() -> None:
    ctx = build_execution_context(surface="chat")
    out = render_system_prompt(["{% raw %}{{ literal }}{% endraw %}"], ctx)
    assert out == "{{ literal }}"
