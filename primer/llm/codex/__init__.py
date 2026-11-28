"""Codex model selection and executor-free integration primitives."""

from primer.llm.codex.models import (
    CODEX_DIRECT_MODELS,
    CODEX_MAX_TESTED_VERSION,
    MINIMUM_VERSION,
    is_direct_model,
)

__all__ = [
    "CODEX_DIRECT_MODELS",
    "CODEX_MAX_TESTED_VERSION",
    "MINIMUM_VERSION",
    "is_direct_model",
]
