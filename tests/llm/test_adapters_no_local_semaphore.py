"""Pin that no LLM adapter retains a local asyncio.Semaphore — all
concurrency is mediated through RateLimiter."""

from __future__ import annotations

import importlib
import inspect

import pytest


@pytest.mark.parametrize("module_path", [
    "matrix.llm.anthropic",
    "matrix.llm.openresponses",
    "matrix.llm.gemini",
    "matrix.llm.ollama",
])
def test_llm_adapter_no_local_semaphore(module_path):
    module = importlib.import_module(module_path)
    source = inspect.getsource(module)
    assert "asyncio.Semaphore(" not in source, (
        f"{module_path} must use RateLimiter, not a local Semaphore."
    )


@pytest.mark.parametrize("module_path,class_name", [
    ("matrix.llm.openresponses", "OpenResponsesLLM"),
    ("matrix.llm.gemini", "GeminiLLM"),
    ("matrix.llm.ollama", "OllamaLLM"),
])
def test_llm_adapter_accepts_rate_limiter_kwarg(module_path, class_name):
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    sig = inspect.signature(cls.__init__)
    assert "rate_limiter" in sig.parameters, (
        f"{class_name}.__init__ must accept rate_limiter kwarg."
    )
