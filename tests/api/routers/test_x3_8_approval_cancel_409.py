"""X3.8: a single-approval answer that arrives after the session's cancel
flipped the park (the cancel stamps ``cancel_requested`` and flips the parked
row to resumable) answers a 409 that names the cancel -- the gate was not
decided by another answer, it was cancelled -- instead of the generic
"already decided". An answer refused for any other reason keeps the generic
409.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import primer.api.routers.tool_approval as tool_approval_mod
from primer.api.routers.tool_approval import (
    ToolApprovalRespondBody,
    _publish_decision,
)


class _Storage:
    def __init__(self, row) -> None:
        self._row = row

    async def get(self, session_id):
        return self._row


async def _refused_flip(sess, **kwargs):
    return False


async def _answer(storage) -> HTTPException:
    with pytest.raises(HTTPException) as excinfo:
        await _publish_decision(
            sess=SimpleNamespace(),
            id_str="s1",
            body=ToolApprovalRespondBody(tool_call_id="tc", decision="approved"),
            gate={"event_key": "k1", "resume_metadata": None},
            event_bus=None,
            session_storage=storage,
            engine=None,
        )
    return excinfo.value


@pytest.mark.asyncio
async def test_an_approval_answer_after_a_cancel_flip_names_the_cancel(monkeypatch) -> None:
    monkeypatch.setattr(tool_approval_mod, "durably_wake_session", _refused_flip)
    exc = await _answer(_Storage(SimpleNamespace(cancel_requested=True)))
    assert exc.status_code == 409
    assert exc.detail["code"] == "already_decided"
    assert "cancelled" in exc.detail["message"]
    assert "s1" in exc.detail["message"]


@pytest.mark.asyncio
async def test_an_answer_refused_for_another_reason_keeps_the_generic_409(monkeypatch) -> None:
    monkeypatch.setattr(tool_approval_mod, "durably_wake_session", _refused_flip)
    exc = await _answer(_Storage(SimpleNamespace(cancel_requested=False)))
    assert exc.status_code == 409
    assert exc.detail["code"] == "already_decided"
    assert "cancelled" not in exc.detail["message"]
