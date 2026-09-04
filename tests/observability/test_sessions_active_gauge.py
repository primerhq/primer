"""S7 section 4: sessions_active is inc'd for the life of a running turn.

The gauge brackets the dispatch streaming region, which is the one exact
chokepoint every started turn passes through (park, error, cancel and
clean exits all run its finally).
"""

from __future__ import annotations

import inspect
import textwrap

import primer.session.dispatch as dispatch


def test_gauge_is_incremented_before_the_stream_and_decremented_after():
    src = inspect.getsource(dispatch.run_one_session_turn)
    inc = src.index("sessions_active.labels(session.workspace_id).inc()")
    dec = src.index("sessions_active.labels(session.workspace_id).dec()")
    assert inc < dec, "inc must bracket the streaming region"


def test_gauge_decrements_inside_a_finally():
    # Structural (AST) check, not source-text slicing: the old version
    # looked for "finally:" within a fixed character window before the
    # dec() call, which broke the moment an unrelated addition to the
    # SAME finally (e2e8ecfb's turn_events.aclose() block) pushed the
    # keyword out of the window - a false failure on correct code. Walk
    # the function's Try nodes instead and assert the dec lives in a
    # finalbody, alongside the cancel-watcher teardown it must share
    # cleanup with.
    import ast

    src = inspect.getsource(dispatch.run_one_session_turn)
    tree = ast.parse(textwrap.dedent(src))
    finals_with_dec = [
        ast.unparse(ast.Module(body=t.finalbody, type_ignores=[]))
        for t in ast.walk(tree)
        if isinstance(t, ast.Try) and t.finalbody
        and "sessions_active" in ast.unparse(
            ast.Module(body=t.finalbody, type_ignores=[])
        )
    ]
    assert finals_with_dec, "sessions_active dec() must sit inside a finally"
    assert any("cancel_task.cancel()" in f for f in finals_with_dec), (
        "the dec belongs in the existing cancel-watcher finally"
    )


def test_gauge_round_trips():
    import primer.observability.metrics as m
    m.reset_for_test()
    try:
        m.sessions_active.labels("ws-1").inc()
        assert m.sessions_active.labels("ws-1")._value.get() == 1.0
        m.sessions_active.labels("ws-1").dec()
        assert m.sessions_active.labels("ws-1")._value.get() == 0.0
    finally:
        m.reset_for_test()


def test_workspace_id_is_the_only_label():
    import primer.observability.metrics as m
    assert m.sessions_active._labelnames == ("workspace_id",)
