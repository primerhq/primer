"""Journey: a live client hears how a turn ENDED over the workspace tap, with no later message to wake it
(console review 2026-10-08, C-008).

The message writer buffers records and dispatch ticks right after each append, so the last records of a turn were announced
while they were still in the buffer; the clean-completion path then flushed and published nothing, and a client following the
tap never received the turn's final ``assistant_token`` and ``done`` until the next message produced another tick (the console
showed the finished turn as still running). This follows the tap of a real server over real HTTP, through the real
OpenChatLLM client against the scripted mock, and asserts the end of the turn arrives while NOTHING else is sent.

Unlike tests/session/test_tap_sees_the_final_flush.py (a unit test with an injected write latency, red on the old code), a local
workspace on a fast disk takes the write before the tap looks, so this journey passes on the old code here too: it guards the
user-visible flow, not the ordering.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import make_local_workspace, make_scripted_agent, start_agent_session

_FRAME_WAIT_S = 20.0


async def _follow_tap(client: httpx.AsyncClient, workspace_id: str, frames: list[dict]) -> None:
    async with client.stream("GET", f"/v1/workspaces/{workspace_id}/tap", timeout=None) as response:
        assert response.status_code == 200, response.status_code
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                with contextlib.suppress(ValueError):
                    frames.append(json.loads(line[6:]))


async def _wait_for(frames: list[dict], session_id: str, frame_class: str) -> dict | None:
    """The first durable ``frame_class`` frame of the session, or None if none arrives in ``_FRAME_WAIT_S``."""
    try:
        async with asyncio.timeout(_FRAME_WAIT_S):
            while True:
                for frame in frames:
                    if frame.get("session_id") == session_id and frame.get("class") == frame_class and frame.get("seq") is not None:
                        return frame
                await asyncio.sleep(0.1)
    except TimeoutError:
        return None


@pytest.mark.asyncio
async def test_the_tap_delivers_a_clean_turns_answer_and_done_without_a_later_message(
    client: httpx.AsyncClient, mock_llm, unique_suffix: str, tmp_path,
) -> None:
    registry, base_url = mock_llm
    scenario = f"scripted:tap-end-{unique_suffix}"
    agent = await make_scripted_agent(
        client, registry, base_url, suffix=unique_suffix, scenario=scenario,
        rules=[Rule(emit_text="Everything is done and nothing else needs saying.", chunk_delay_s=0.1, text_chunk_words=2)],
    )
    workspace_id = await make_local_workspace(client, suffix=unique_suffix, root=tmp_path)

    frames: list[dict] = []
    follower = asyncio.create_task(_follow_tap(client, workspace_id, frames))
    try:
        # A live server sends the response headers with the first frame (the middleware holds them), so there is nothing to await
        # for "connected". The route subscribes to the router while it handles the request, and a tick published before that is
        # dropped (ticks are advisory), so give it a moment and check the stream did not die on the way.
        await asyncio.sleep(1.5)
        assert not follower.done(), follower.exception() if not follower.cancelled() else "cancelled"
        session_id = await start_agent_session(client, workspace_id=workspace_id, agent_id=agent["agent_id"], instructions="hello")

        answer = await _wait_for(frames, session_id, "assistant_token")
        done = await _wait_for(frames, session_id, "done")
        assert answer is not None and done is not None, (
            f"the tap delivered {[(f.get('session_id'), f.get('class'), f.get('seq')) for f in frames]} for {session_id}"
        )
        assert answer["seq"] < done["seq"], "the answer precedes the done record"
        assert "Everything is done" in json.dumps(answer.get("payload")), answer
    finally:
        follower.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await follower
