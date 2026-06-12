import pytest

from primer.agent.invoke import (
    InvocationDepthExceeded, MAX_INVOCATION_DEPTH, invocation_depth_guard,
)


def test_depth_guard_increments_and_resets():
    assert _depth() == 0
    with invocation_depth_guard():
        assert _depth() == 1
        with invocation_depth_guard():
            assert _depth() == 2
    assert _depth() == 0


def test_depth_guard_raises_past_max():
    import contextlib
    stack = contextlib.ExitStack()
    for _ in range(MAX_INVOCATION_DEPTH):
        stack.enter_context(invocation_depth_guard())
    with pytest.raises(InvocationDepthExceeded):
        with invocation_depth_guard():
            pass
    stack.close()
    assert _depth() == 0


def _depth():
    from primer.agent.invoke import _DEPTH
    return _DEPTH.get()
