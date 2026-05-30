"""Verify the ABC requires count_tokens on every subclass."""

from __future__ import annotations

import inspect

import pytest

from primer.int.llm import LLM


class TestLLMABC:
    def test_count_tokens_is_abstract(self) -> None:
        assert "count_tokens" in LLM.__abstractmethods__

    def test_count_tokens_signature(self) -> None:
        sig = inspect.signature(LLM.count_tokens)
        params = sig.parameters
        assert "model" in params
        assert "messages" in params
        assert "tools" in params

    def test_subclass_without_count_tokens_cannot_instantiate(self) -> None:
        class Broken(LLM):
            async def list_models(self):
                return []
            def stream(self, **kw):
                async def _g():
                    yield
                return _g()
        with pytest.raises(TypeError, match="count_tokens"):
            Broken()
