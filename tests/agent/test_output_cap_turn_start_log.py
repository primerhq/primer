"""A turn LOGS a warning at its start when the agent's output cap fills the model's window (01a10c6b item 2).

The executor's guard (``output_cap_never_fits``) is reactive: it acts after a provider rejected the prompt. The first
call is spent before it, and an operator reading the log would see only the rejection. A turn that starts with a cap that
is not below the window says so up front, once per turn, with the numbers. It does not refuse: a server that clamps an
oversized cap serves the turn.

An AGGREGATED profile is skipped: the executor only knows the MIN member window, and a cap between the windows is
served by the larger member (see the status endpoint, which checks the largest).
"""

from __future__ import annotations

import logging

import pytest

from primer.model.chat import Message, TextDelta, TextPart
from primer.model.model_profile import ModelProfileConfig
from primer.model_profile import ResolvedModel
from tests.agent.test_overflow_recovery import MODEL, _Executor, _FailsThenAnswers, _SpyCompaction

LOGGER = "primer.agent.base"


async def _turn(executor: _Executor) -> None:
    async for _ in executor.invoke([Message(role="user", parts=[TextPart(text="go")])]):
        pass


def _cap_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER and "output cap" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_turn_with_a_cap_that_fills_the_window_logs_it_once_and_still_runs(caplog) -> None:
    llm = _FailsThenAnswers(failures=0)
    executor = _Executor(llm, _SpyCompaction(), max_output_tokens=MODEL.context_length)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        events = []
        async for ev in executor.invoke([Message(role="user", parts=[TextPart(text="go")])]):
            events.append(ev)

    found = _cap_warnings(caplog)
    assert len(found) == 1, f"expected one turn-start warning, got {[r.getMessage() for r in caplog.records]}"
    assert found[0].levelno == logging.WARNING
    assert (found[0].max_output_tokens, found[0].context_length) == (4096, 4096)  # type: ignore[attr-defined]
    assert llm.calls == 1 and any(isinstance(e, TextDelta) for e in events), "the turn must still run"


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [1024, MODEL.context_length - 1, None], ids=["well-below", "one-below", "unset"])
async def test_a_cap_below_the_window_or_unset_logs_nothing(caplog, cap) -> None:
    executor = _Executor(_FailsThenAnswers(failures=0), _SpyCompaction(), max_output_tokens=cap)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _turn(executor)

    assert _cap_warnings(caplog) == []


@pytest.mark.asyncio
async def test_an_aggregated_profile_is_not_checked_against_its_min_window(caplog) -> None:
    executor = _Executor(_FailsThenAnswers(failures=0), _SpyCompaction(), max_output_tokens=10_000)
    executor._model = ResolvedModel(  # type: ignore[attr-defined]
        profile_id="agg", provider_id=None, model_name=None, context_length=8192, config=ModelProfileConfig(),
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _turn(executor)

    assert _cap_warnings(caplog) == []
