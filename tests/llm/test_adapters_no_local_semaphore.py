"""Pin that no LLM/embedder/cross-encoder adapter retains a local
asyncio.Semaphore — all concurrency is mediated through RateLimiter."""

from __future__ import annotations

import importlib
import inspect

import pytest


_ADAPTER_MODULES = [
    "matrix.llm.anthropic",
    "matrix.llm.openresponses",
    "matrix.llm.gemini",
    "matrix.llm.ollama",
    "matrix.embedder.openai",
    "matrix.embedder.gemini",
    "matrix.embedder.huggingface",
    "matrix.cross_encoder.huggingface",
]


@pytest.mark.parametrize("module_path", _ADAPTER_MODULES)
def test_adapter_no_local_semaphore(module_path):
    module = importlib.import_module(module_path)
    source = inspect.getsource(module)
    assert "asyncio.Semaphore(" not in source, (
        f"{module_path} must use RateLimiter, not a local Semaphore."
    )


_KWARG_TARGETS = [
    ("matrix.llm.openresponses", "OpenResponsesLLM"),
    ("matrix.llm.gemini", "GeminiLLM"),
    ("matrix.llm.ollama", "OllamaLLM"),
]


@pytest.mark.parametrize("module_path,class_name", _KWARG_TARGETS)
def test_llm_adapter_accepts_rate_limiter_kwarg(module_path, class_name):
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    sig = inspect.signature(cls.__init__)
    assert "rate_limiter" in sig.parameters
