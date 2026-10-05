"""The webhook hold's final text of a turn that did NOT complete, through the real dispatch.

``tests/channel/test_session_final_text_window.py`` pins ``derive_session_final_text`` on hand-built records. This
pins it on the records ``run_one_session_turn`` actually writes: a turn that was stopped, one whose Cancel landed
after the model's terminal event, and one that failed all leave partial or complete assistant text in the log, and
none of them may hand that text to the hold as the run's result.
"""

from __future__ import annotations

import asyncio

import pytest

from primer.channel.session_relay import read_session_final_text
from primer.model.chat import Done, TextDelta
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
    seeded_session,
)
from tests.session import test_dispatch_interrupt as interrupt_tests  # the module, so its test classes are not collected here

_records = interrupt_tests._records
_request_stop = interrupt_tests._request_stop
_run = interrupt_tests._run
_StopAwareExecutor = interrupt_tests._StopAwareExecutor


@pytest.mark.parametrize("how", ["a Stop mid-stream", "a Cancel after the model finished", "a failed turn"])
async def test_a_turn_that_did_not_complete_has_no_final_text(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, how,
) -> None:
    sid = seeded_session.id
    cancel_lands = interrupt_tests.TestACancelThatLandsAfterTheModelFinished()._cancel_lands

    async def stop_lands() -> None:
        await _request_stop(fake_storage_provider, fake_event_bus, sid)
        await asyncio.sleep(0.1)

    async def cancel_lands_mid_turn() -> None:
        await cancel_lands(fake_storage_provider, fake_event_bus, sid)
        await asyncio.sleep(0.1)

    if how == "a Stop mid-stream":
        script = [TextDelta(text="a partial answer", index=0), stop_lands, "BLOCK"]
    elif how == "a Cancel after the model finished":
        script = [TextDelta(text="a complete answer", index=0), cancel_lands_mid_turn,
                  Done(stop_reason="stop", raw_reason="stop")]
    else:
        script = [TextDelta(text="half an ans", index=0), Done(stop_reason="error", raw_reason="error")]

    await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, _StopAwareExecutor(script), sid)

    assert any(r["kind"] == "assistant_token" for r in _records(fake_workspace_io, sid)), (
        "the turn left no assistant text in the log, so this test cannot tell the cases apart"
    )
    assert await read_session_final_text(fake_workspace_io, sid) is None
