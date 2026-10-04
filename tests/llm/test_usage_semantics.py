"""What ``Usage.input_tokens`` means, per provider kind, pinned from payloads.

``Usage.input_tokens`` is documented as the WHOLE prompt the provider processed,
cached tokens included, and ``cached_input_tokens`` as a subset of it. A consumer
that reads the provider's own usage as the size of the prompt (the live compaction
trigger will) is only right where an adapter honours that. These tests pin each
adapter's mapping from a payload shaped like the vendor's documentation, so a
semantic change in an adapter fails here instead of quietly shifting every
decision made from usage.

Sources (fetched 2026-10-04): Anthropic prompt-caching docs ("total_input_tokens =
cache_read_input_tokens + cache_creation_input_tokens + input_tokens"; the API's
``input_tokens`` EXCLUDES cached tokens); Gemini ``UsageMetadata`` ("promptTokenCount
... is still the total effective prompt size, including the cached content"); the
OpenAI usage objects (``prompt_tokens`` / ``input_tokens`` include cached tokens, with
``cached_tokens`` a subset). NOT VERIFIED against live servers: OpenRouter, LM Studio,
llama.cpp, vLLM (all through the OpenChat mapping) and Ollama.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.llm import _openai_compat, gemini, ollama, openresponses
from primer.llm import anthropic as anthropic_mod
from primer.model.chat import Usage


def _anthropic_usage(*, start: dict, delta: dict | None = None, output: int = 20) -> Usage:
    state = anthropic_mod._StreamState()
    anthropic_mod._translate_event(
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(id="m1", model="claude", usage=SimpleNamespace(**start)),
        ),
        state, model_name="claude",
    )
    anthropic_mod._translate_event(
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(output_tokens=output, **(delta or {})),
        ),
        state, model_name="claude",
    )
    events = anthropic_mod._translate_event(SimpleNamespace(type="message_stop"), state, model_name="claude")
    return next(e for e in events if isinstance(e, Usage))


class TestAnthropic:
    def test_a_cache_read_is_part_of_the_prompt(self) -> None:
        # The documentation's own example: 100,000 cached + 50 after the breakpoint.
        usage = _anthropic_usage(start={
            "input_tokens": 50, "cache_read_input_tokens": 100_000,
            "cache_creation_input_tokens": 0,
        })
        assert usage.input_tokens == 100_050
        assert usage.cached_input_tokens == 100_000

    def test_cache_creation_is_part_of_the_prompt_but_not_served_from_cache(self) -> None:
        usage = _anthropic_usage(start={
            "input_tokens": 10, "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 2_000,
        })
        assert usage.input_tokens == 2_510
        assert usage.cached_input_tokens == 500

    def test_without_caching_input_tokens_is_the_prompt(self) -> None:
        usage = _anthropic_usage(start={"input_tokens": 1_234})
        assert (usage.input_tokens, usage.cached_input_tokens) == (1_234, None)

    def test_a_delta_reporting_zero_input_tokens_does_not_zero_the_prompt(self) -> None:
        """Cumulative counters never decrease; a message_delta carrying
        input_tokens=0 (and zero cache fields) must not erase the start report."""
        usage = _anthropic_usage(
            start={"input_tokens": 50, "cache_read_input_tokens": 1_000},
            delta={"input_tokens": 0, "cache_read_input_tokens": 0},
        )
        assert usage.input_tokens == 1_050
        assert usage.cached_input_tokens == 1_000

    def test_a_later_cumulative_report_raises_the_start_report(self) -> None:
        usage = _anthropic_usage(
            start={"input_tokens": 5, "cache_read_input_tokens": 100},
            delta={"input_tokens": 7, "cache_read_input_tokens": 120},
        )
        assert usage.input_tokens == 127
        assert usage.cached_input_tokens == 120

    def test_a_pre_caching_sdk_object_with_no_cache_fields_still_works(self) -> None:
        usage = _anthropic_usage(start={"input_tokens": 42})
        assert usage.input_tokens == 42


class TestGemini:
    def test_prompt_token_count_already_includes_the_cached_content(self) -> None:
        usage = gemini._build_usage(SimpleNamespace(
            prompt_token_count=1_000, cached_content_token_count=800,
            candidates_token_count=50, thoughts_token_count=30,
        ))
        assert usage.input_tokens == 1_000
        assert usage.cached_input_tokens == 800
        assert usage.reasoning_tokens == 30


class TestOpenAIFamily:
    def test_responses_input_tokens_includes_cached_tokens(self) -> None:
        usage = openresponses._build_usage(SimpleNamespace(
            input_tokens=1_000, output_tokens=40,
            input_tokens_details=SimpleNamespace(cached_tokens=600),
            output_tokens_details=SimpleNamespace(reasoning_tokens=10),
        ))
        assert usage.input_tokens == 1_000
        assert usage.cached_input_tokens == 600

    def test_chat_completions_prompt_tokens_is_the_prompt(self) -> None:
        """OpenAI spec; local servers and OpenRouter are UNVERIFIED (see module doc)."""
        usage = _openai_compat._build_usage(
            SimpleNamespace(prompt_tokens=1_000, completion_tokens=40),
        )
        assert usage.input_tokens == 1_000


class TestOllama:
    def test_prompt_eval_count_is_mapped_but_is_not_a_full_prompt_guarantee(self) -> None:
        """Pins the mapping only. Ollama documents ``prompt_eval_count`` as the number
        of tokens in the prompt and lists ``prompt_eval_cached_count`` separately;
        third-party reports say a warm KV cache lowers it and that a prompt larger
        than the loaded num_ctx is silently truncated and reported post-truncation.
        Not reproduced here, so provider usage from Ollama must not be trusted as
        the prompt size until a live-server probe pins it."""
        state = ollama._StreamState()
        events = ollama._translate_chunk(
            SimpleNamespace(
                done=True, done_reason="stop", prompt_eval_count=26, eval_count=9,
                message=None,
            ),
            state,
            model_name="llama3",
        )
        usage = next(e for e in events if isinstance(e, Usage))
        assert usage.input_tokens == 26


@pytest.mark.parametrize(
    "usage",
    [
        _anthropic_usage(start={"input_tokens": 50, "cache_read_input_tokens": 100}),
        gemini._build_usage(SimpleNamespace(
            prompt_token_count=10, cached_content_token_count=4, candidates_token_count=1,
        )),
        openresponses._build_usage(SimpleNamespace(
            input_tokens=10, output_tokens=1,
            input_tokens_details=SimpleNamespace(cached_tokens=4),
        )),
    ],
)
def test_cached_tokens_are_a_subset_of_input_tokens(usage: Usage) -> None:
    assert usage.cached_input_tokens is not None
    assert usage.cached_input_tokens <= usage.input_tokens
