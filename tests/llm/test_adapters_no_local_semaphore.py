"""Pin that no LLM/embedder/cross-encoder adapter retains a local
asyncio.Semaphore — all concurrency is mediated through RateLimiter."""

from __future__ import annotations

import importlib
import inspect

import pytest


_ADAPTER_MODULES = [
    "primer.llm.anthropic",
    "primer.llm.openresponses",
    "primer.llm.gemini",
    "primer.llm.ollama",
    "primer.embedder.openai",
    "primer.embedder.gemini",
    "primer.embedder.huggingface",
    "primer.cross_encoder.huggingface",
]


@pytest.mark.parametrize("module_path", _ADAPTER_MODULES)
def test_adapter_no_local_semaphore(module_path):
    module = importlib.import_module(module_path)
    source = inspect.getsource(module)
    assert "asyncio.Semaphore(" not in source, (
        f"{module_path} must use RateLimiter, not a local Semaphore."
    )


_KWARG_TARGETS = [
    ("primer.llm.openresponses", "OpenResponsesLLM"),
    ("primer.llm.gemini", "GeminiLLM"),
    ("primer.llm.ollama", "OllamaLLM"),
]


@pytest.mark.parametrize("module_path,class_name", _KWARG_TARGETS)
def test_llm_adapter_accepts_rate_limiter_kwarg(module_path, class_name):
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    sig = inspect.signature(cls.__init__)
    assert "rate_limiter" in sig.parameters
