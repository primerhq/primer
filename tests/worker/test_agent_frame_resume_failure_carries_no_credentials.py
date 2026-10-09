"""A subagent continuation whose LLM stream failed reports the failure to the parent without the credential it carried (#676 review round 1, B3).

``AgentFrame.resume`` turns a ``TurnStreamFailure`` into an error tool result for the parent turn (``primer/worker/frames.py``). The stream's message is a
provider's own text and can quote the Authorization header it received.
"""

from __future__ import annotations

import json

import pytest

from primer.model.chat import Error, TurnStreamFailure
from primer.worker.frames import AgentFrame, AgentResumeContext, Completed

BEARER = "sk-bearer-ABCDEFGH12345678"
URL = "https://svc-user:hunter2pw@gateway.internal/v1/chat?api_key=SKSECRET123456"


class _Services:
    def __init__(self, error: Error) -> None:
        self._error = error

    async def resume_subagent(self, **_kwargs):
        raise TurnStreamFailure(self._error, partial_messages=[], rounds_completed=1)


def _frame() -> AgentFrame:
    return AgentFrame(
        agent_id="sub", llm_messages=[{"role": "assistant", "parts": []}], tool_call_id="invoke-tc", depth=0,
        context=AgentResumeContext(session_id="s", workspace_id="w", chat_id=None, principal="p", tools=[]),
    )


@pytest.mark.asyncio
async def test_a_failed_subagent_stream_is_reported_without_its_credentials() -> None:
    error = Error(code="unauthorized", message=f"refused: Authorization: Bearer {BEARER} for url '{URL}'", fatal=True)

    outcome = await _frame().resume("child-result", _Services(error))

    assert isinstance(outcome, Completed) and outcome.value.error is True
    text = outcome.value.output
    assert BEARER not in text and "hunter2pw" not in text and "SKSECRET123456" not in text
    assert "subagent LLM stream failed" in json.loads(text)["error"], "the model still learns what failed"
