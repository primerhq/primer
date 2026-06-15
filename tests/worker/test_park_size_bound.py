"""Task 7.2 - park-size bound for a max-depth nested-yield continuation.

A nested invoke_agent park serialises the WHOLE caller chain into the
``parked_state`` JSONB blob: one :class:`~primer.worker.frames.AgentFrame` per
in-flight caller, each carrying its own mid-flight ``llm_messages``. The depth is
bounded by ``MAX_INVOCATION_DEPTH`` (the invocation-depth guard in
``primer.agent.invoke``), so the park blob is bounded too. This test pins that
bound: it builds a worst-case ``MAX_INVOCATION_DEPTH``-deep stack of frames each
carrying a representative (non-trivial) message history, wraps it in a
:class:`~primer.worker.yield_runtime.ParkedState`, and asserts the JSON blob
serialises and stays under a documented size.

Measured size at MAX_INVOCATION_DEPTH=8 with the representative history below:
**7160 bytes**. The bound is set to **12000 bytes** (~1.67x headroom over the
measured size, comfortably above the 1.5x = 10740 floor) so realistic message
growth per frame does not silently bust the JSONB park column, while still
catching a regression that bloats the per-frame payload by an order of
magnitude. If MAX_INVOCATION_DEPTH or the frame schema grows, re-measure and
re-document here: the depth/size tradeoff is intentional and visible.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from primer.agent.invoke import MAX_INVOCATION_DEPTH
from primer.model.chat import Message, TextPart, ToolCallPart
from primer.model.yield_ import Yielded
from primer.worker.frames import AgentFrame, AgentResumeContext
from primer.worker.yield_runtime import ParkedState


# Measured worst-case at MAX_INVOCATION_DEPTH=8 (see module docstring): 7160 B.
_MEASURED_BYTES = 7160
# Documented bound: ~1.67x the measured size (well above the 1.5x = 10740 floor).
_PARK_SIZE_BOUND = 12_000


def _representative_messages(i: int) -> list[dict]:
    """A non-trivial two-message mid-flight history with realistic text.

    One user instruction + one assistant turn that emits the invoke_agent
    tool_use that parked - the shape a real nested caller frame carries.
    """
    user = Message(
        role="user",
        parts=[
            TextPart(
                text=(
                    "Please research the quarterly figures and summarise the top "
                    f"three risks for region {i}. Be thorough and cite sources."
                )
            )
        ],
    )
    assistant = Message(
        role="assistant",
        parts=[
            TextPart(
                text=(
                    f"I will analyse the data for region {i} now, breaking it into "
                    "revenue, churn, and pipeline. Delegating to the sub-analyst."
                )
            ),
            ToolCallPart(
                id=f"call-{i}",
                name="system__invoke_agent",
                arguments={"agent_id": "analyst", "prompt": f"analyse region {i}"},
            ),
        ],
    )
    return [user.model_dump(mode="json"), assistant.model_dump(mode="json")]


def _max_depth_parked_state() -> ParkedState:
    frames = [
        AgentFrame(
            agent_id=f"agent-analyst-{d}",
            llm_messages=_representative_messages(d),
            tool_call_id=f"invoke-tc-{d}",
            depth=d,
            context=AgentResumeContext(
                session_id="sess-deep",
                workspace_id="ws-deep",
                chat_id=None,
                principal="user-1",
                tools=["system__invoke_agent", "t1__do_it", "misc__ask_user"],
            ),
        )
        for d in range(MAX_INVOCATION_DEPTH)
    ]
    leaf = Yielded(
        tool_name="ask_user",
        event_key="ask_user:sess-deep:leaf",
        resume_metadata={
            "prompt": "final question?",
            "parked_at_iso": datetime.now(timezone.utc).isoformat(),
        },
    )
    return ParkedState(
        yielded=leaf,
        llm_messages=_representative_messages(99),
        turn_no=3,
        started_at=datetime.now(timezone.utc),
        tool_call_id="invoke-tc-0",
        frames=frames,
    )


def test_max_depth_park_serialises_under_bound():
    parked = _max_depth_parked_state()

    # The full max-depth stack is present in the serialised blob.
    blob = parked.to_jsonable()
    assert len(blob["frames"]) == MAX_INVOCATION_DEPTH

    serialised = json.dumps(blob)
    size = len(serialised)

    # Serialises cleanly and round-trips (no data loss at max depth).
    rehydrated = ParkedState.from_jsonable(blob)
    assert len(rehydrated.frames) == MAX_INVOCATION_DEPTH
    assert rehydrated.frames[0].tool_call_id == "invoke-tc-0"

    # Stays under the documented bound. The measured size is recorded so the
    # depth/size tradeoff is visible; the bound carries headroom for realistic
    # per-frame message growth while still catching an order-of-magnitude bloat.
    assert size < _PARK_SIZE_BOUND, (
        f"max-depth park is {size} bytes, over the documented bound of "
        f"{_PARK_SIZE_BOUND} (measured baseline {_MEASURED_BYTES})"
    )
