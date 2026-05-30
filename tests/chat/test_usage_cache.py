"""Per-chat Usage cache."""

from __future__ import annotations

import pytest

from primer.chat.usage_cache import (
    clear_usage,
    get_usage,
    reset_cache,
    set_usage,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_cache()


def test_get_returns_zeros_when_not_set() -> None:
    assert get_usage("never") == {"input_tokens": 0, "output_tokens": 0}


def test_set_then_get() -> None:
    set_usage("c1", input_tokens=1234, output_tokens=56)
    assert get_usage("c1") == {"input_tokens": 1234, "output_tokens": 56}


def test_clear_resets_to_zero() -> None:
    set_usage("c1", input_tokens=10, output_tokens=2)
    clear_usage("c1")
    assert get_usage("c1") == {"input_tokens": 0, "output_tokens": 0}


def test_chats_isolated() -> None:
    set_usage("a", input_tokens=1, output_tokens=1)
    set_usage("b", input_tokens=2, output_tokens=2)
    assert get_usage("a")["input_tokens"] == 1
    assert get_usage("b")["input_tokens"] == 2
