"""M4 / T9: what a round of a turn recomputes (token estimates, prune keys, the system prompt) is computed once per distinct input."""

from __future__ import annotations

import hashlib
import json

import pytest

import primer.agent.prune as prune_mod
import primer.llm._tokenizer.char_fallback as cf
from primer.agent import prompt_render
from primer.agent.compaction import CompactionStrategy
from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback
from primer.model.except_ import BadRequestError
from tests.perf._turn_fixture import history, tools

# Captured from the code before the memoisation: the estimates must not move by a token.
GOLDEN_WITH_TOOLS = 14292
GOLDEN_MESSAGES_ONLY = 7551


class _CountingJson:
    def __init__(self):
        self.dumps_calls = 0

    def dumps(self, *a, **kw):
        self.dumps_calls += 1
        return json.dumps(*a, **kw)


def test_estimates_are_byte_for_byte_the_golden_numbers():
    h, t = history(), tools()
    assert count_tokens_char_fallback(messages=h, tools=t) == GOLDEN_WITH_TOOLS
    assert count_tokens_char_fallback(messages=h) == GOLDEN_MESSAGES_ONLY
    assert CompactionStrategy._estimate_tokens(h) == GOLDEN_MESSAGES_ONLY
    # a second pass over the same objects (the next round) gives the same numbers
    assert count_tokens_char_fallback(messages=h, tools=t) == GOLDEN_WITH_TOOLS


def test_json_dumps_is_called_per_distinct_tool_and_call_part_not_per_round(monkeypatch):
    counter = _CountingJson()
    monkeypatch.setattr(cf, "json", counter)
    h, t = history(), tools()
    call_parts = sum(1 for m in h for p in m.parts if p.__class__.__name__ == "ToolCallPart")
    for _ in range(5):
        count_tokens_char_fallback(messages=h, tools=t)
        CompactionStrategy._estimate_tokens(h)
    assert counter.dumps_calls <= call_parts + len(t)


def test_a_changed_tool_schema_is_not_served_from_the_memo():
    t = tools(3)
    before = count_tokens_char_fallback(messages=[], tools=t)
    t[0].args_schema = {"type": "object", "properties": {"zz": {"type": "string", "description": "d" * 400}}}
    assert count_tokens_char_fallback(messages=[], tools=t) > before


def test_prune_keys_hash_each_distinct_output_once(monkeypatch):
    calls = []
    real = hashlib.sha256

    def counting(data=b"", **kw):
        calls.append(1)
        return real(data, **kw)

    monkeypatch.setattr(prune_mod.hashlib, "sha256", counting)
    h = history()
    first = prune_mod.result_keys(h)
    assert first is not None
    n = len(calls)
    for _ in range(4):
        assert prune_mod.result_keys(h) == first
    assert len(calls) == n


class _Ctx:
    pass


def test_the_system_prompt_template_is_compiled_once_per_text(monkeypatch):
    compiles = []
    real = prompt_render._ENV.from_string
    monkeypatch.setattr(prompt_render._ENV, "from_string", lambda text, *a, **kw: (compiles.append(text), real(text, *a, **kw))[1])
    prompt_render._compiled.cache_clear()
    for _ in range(5):
        assert prompt_render.render_system_prompt(["You are {{ 1 + 1 }}.", "Second."], _Ctx()) == "You are 2.\n\nSecond."
    assert len(compiles) == 1


def test_template_errors_keep_their_type_and_message():
    with pytest.raises(BadRequestError, match="template syntax error at line 1"):
        prompt_render.render_system_prompt(["{% if %}"], _Ctx())
    with pytest.raises(BadRequestError, match="render error"):
        prompt_render.render_system_prompt(["{{ ctx.missing_attribute }}"], _Ctx())
    # and again: an error is not cached into a success
    with pytest.raises(BadRequestError, match="template syntax error"):
        prompt_render.render_system_prompt(["{% if %}"], _Ctx())
