"""YieldToWorker.frames default attribute (unified nested-yield resume)."""

from primer.model.yield_ import YieldToWorker, Yielded


def _yld():
    return YieldToWorker(
        Yielded(tool_name="ask_user", event_key="ask_user:ses:c1"),
        tool_call_id="c1",
    )


def test_fresh_yield_has_empty_frames():
    yld = _yld()
    assert yld.frames == []


def test_frames_is_appendable():
    yld = _yld()
    yld.frames.append("frame-a")
    yld.frames = ["frame-b"] + yld.frames
    assert yld.frames == ["frame-b", "frame-a"]
