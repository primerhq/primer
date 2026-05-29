"""Unit tests for the shared openai common helpers."""

from __future__ import annotations

import logging

import pytest

from primer.llm._openai_common import build_sampling_params


class TestBuildSamplingParamsResponsesTarget:
    def test_all_params_forwarded(self) -> None:
        params = build_sampling_params(
            temperature=0.7,
            top_p=0.9,
            max_output_tokens=500,
            stop=None,
            target="responses",
        )
        assert params == {
            "temperature": 0.7,
            "top_p": 0.9,
            "max_output_tokens": 500,
        }

    def test_all_none_returns_empty_dict(self) -> None:
        assert build_sampling_params(
            temperature=None,
            top_p=None,
            max_output_tokens=None,
            stop=None,
            target="responses",
        ) == {}

    def test_stop_silently_dropped_with_warning_for_responses(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="primer.llm._openai_common")
        out = build_sampling_params(
            temperature=None,
            top_p=None,
            max_output_tokens=None,
            stop=["\n", "END"],
            target="responses",
        )
        assert "stop" not in out
        records = [r for r in caplog.records if "stop" in r.message.lower()]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING


class TestBuildSamplingParamsChatCompletionsTarget:
    def test_max_tokens_key_used_instead_of_max_output_tokens(self) -> None:
        params = build_sampling_params(
            temperature=0.3,
            top_p=None,
            max_output_tokens=128,
            stop=None,
            target="chat_completions",
        )
        assert params == {"temperature": 0.3, "max_tokens": 128}

    def test_stop_passes_through_native_with_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="primer.llm._openai_common")
        out = build_sampling_params(
            temperature=None,
            top_p=None,
            max_output_tokens=None,
            stop=["\n", "END"],
            target="chat_completions",
        )
        assert out == {"stop": ["\n", "END"]}
        assert not any("stop" in r.message.lower() for r in caplog.records)

    def test_all_none_returns_empty_dict(self) -> None:
        assert build_sampling_params(
            temperature=None,
            top_p=None,
            max_output_tokens=None,
            stop=None,
            target="chat_completions",
        ) == {}
