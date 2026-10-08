"""Journey: a model that dies mid-answer leaves the half answer in the log, ahead of the error (found 2026-10-08, PR 480).

A real server over HTTP and the real OpenChatLLM client against a scripted mock that streams text and then sends an in-stream error
event. The durable log used to be ``user_input, llm_call, error, error, error``: the text the user watched stream in was gone after a
refresh, because only a Stop or a Cancel ever flushed the coalesce buffers.
"""

from __future__ import annotations

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import make_local_workspace, make_scripted_agent, start_agent_session, wait_terminal

_ANSWER = "Here is the start of an answer that the model never finished"


@pytest.mark.asyncio
async def test_the_text_streamed_before_a_mid_answer_failure_is_in_the_log_before_the_error(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path,
) -> None:
    registry, base_url = mock_llm
    scenario = f"scripted:dies-mid-answer-{unique_suffix}"
    agent = await make_scripted_agent(
        client, registry, base_url, suffix=unique_suffix, scenario=scenario,
        rules=[Rule(emit_text=_ANSWER, fail_mid_stream="upstream melted down")],
    )
    workspace_id = await make_local_workspace(client, suffix=unique_suffix, root=tmp_path)
    session_id = await start_agent_session(client, workspace_id=workspace_id, agent_id=agent["agent_id"], instructions="answer me")

    row = await wait_terminal(client, session_id)
    assert row["status"] == "ended" and row["ended_reason"] == "failed", row

    records = (await client.get(f"/v1/sessions/{session_id}/messages")).json()["items"]
    kinds = [r["kind"] for r in records]
    assert "assistant_token" in kinds, f"the half answer is gone from the log: {kinds}"
    assert kinds.index("assistant_token") < kinds.index("error"), f"the text must land before the first error record: {kinds}"
    text = "".join(r["payload"]["text"] for r in records if r["kind"] == "assistant_token")
    assert text == _ANSWER
