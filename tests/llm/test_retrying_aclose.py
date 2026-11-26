"""RetryingLLM.stream passes the consumer's aclose() through: the inner generator's cleanup (its concurrency slot) has run when aclose() returns,
so a retry opened at once (``_stream_with_fallback`` closes a refused stream and re-opens) is not queued behind an orphaned slot."""

from __future__ import annotations

import pytest

from primer.llm.retrying import RetryingLLM
from primer.model.chat import Done, StreamStart, TextDelta
from primer.model.provider import Limits, LLMProviderType


class _Inner:
    def __init__(self) -> None:
        self.cleaned = 0

    async def stream(self, **kwargs):
        try:
            yield StreamStart(model="m")
            yield TextDelta(text="a", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")
        finally:
            self.cleaned += 1


def _provider(retries: int):
    from types import SimpleNamespace

    return SimpleNamespace(
        id="p", provider=LLMProviderType.OPENCHAT,
        limits=Limits(max_concurrency=1, max_retries=retries),
    )


@pytest.mark.parametrize("retries", [0, 2])
async def test_a_consumer_aclose_runs_the_inner_cleanup_before_it_returns(retries):
    inner = _Inner()
    llm = RetryingLLM(inner, _provider(retries))  # type: ignore[arg-type]
    agen = llm.stream(model="m", messages=[])
    assert isinstance(await agen.__anext__(), StreamStart)
    assert inner.cleaned == 0
    await agen.aclose()
    assert inner.cleaned == 1  # no loop tick in between: the slot is free for the very next attempt
