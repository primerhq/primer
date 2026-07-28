"""The host builds every event key. A tool never names one."""

from __future__ import annotations

import pytest

from primer.model.yield_ import ToolContext
from primer.toolset.python_runner.yielding import YieldKindError, to_yielded

CTX = ToolContext(tool_call_id="tc-1", session_id="s-1", workspace_id=None)


def test_ask_user_key_is_built_from_the_real_context() -> None:
    y = to_yielded(
        {"kind": "ask_user", "params": {"question": "?"}, "meta": {}},
        tool_name="ask",
        ctx=CTX,
        source_version=3,
    )
    assert y.event_key == "ask_user:s-1:tc-1"


def test_timer_key_uses_the_tool_call_id() -> None:
    y = to_yielded(
        {"kind": "timer", "params": {"seconds": 5}, "meta": {}},
        tool_name="nap",
        ctx=CTX,
        source_version=1,
    )
    assert y.event_key == "timer:tc-1"
    assert y.timeout == 5


def test_watch_key_is_session_scoped() -> None:
    y = to_yielded(
        {"kind": "watch", "params": {"paths": ["a.txt"]}, "meta": {}},
        tool_name="w",
        ctx=CTX,
        source_version=1,
    )
    assert y.event_key == "watch:s-1:tc-1"


def test_a_forged_event_key_is_ignored() -> None:
    # The whole point: a tool that could name its own key could resume a park
    # belonging to another session and answer a question asked of someone else.
    y = to_yielded(
        {
            "kind": "ask_user",
            "params": {"question": "?"},
            "meta": {},
            "event_key": "ask_user:victim-session:tc-9",
        },
        tool_name="ask",
        ctx=CTX,
        source_version=1,
    )
    assert y.event_key == "ask_user:s-1:tc-1"
    assert "victim" not in y.event_key


def test_an_unknown_kind_is_rejected() -> None:
    with pytest.raises(YieldKindError):
        to_yielded(
            {"kind": "exec", "params": {}, "meta": {}},
            tool_name="x",
            ctx=CTX,
            source_version=1,
        )


def test_a_missing_kind_is_rejected() -> None:
    with pytest.raises(YieldKindError):
        to_yielded({"params": {}}, tool_name="x", ctx=CTX, source_version=1)


def test_the_source_version_is_pinned_into_resume_metadata() -> None:
    y = to_yielded(
        {"kind": "ask_user", "params": {"question": "?"}, "meta": {"n": 1}},
        tool_name="ask",
        ctx=CTX,
        source_version=7,
    )
    assert y.resume_metadata["source_version"] == 7
    assert y.resume_metadata["tool_meta"] == {"n": 1}


def test_the_tool_name_is_stamped() -> None:
    y = to_yielded(
        {"kind": "ask_user", "params": {"question": "?"}, "meta": {}},
        tool_name="ask",
        ctx=CTX,
        source_version=1,
    )
    assert y.tool_name == "ask"


def test_a_nonsense_timer_duration_falls_back_to_the_global_cap() -> None:
    # None means "use the configured yield-timeout cap" rather than parking
    # forever on a negative or unparseable value.
    for bad in ("soon", -5, 0, None):
        y = to_yielded(
            {"kind": "timer", "params": {"seconds": bad}, "meta": {}},
            tool_name="nap",
            ctx=CTX,
            source_version=1,
        )
        assert y.timeout is None, bad


def test_resume_metadata_is_json_serialisable() -> None:
    import json

    y = to_yielded(
        {"kind": "ask_user", "params": {"question": "?"}, "meta": {"a": [1, 2]}},
        tool_name="ask",
        ctx=CTX,
        source_version=1,
    )
    # The park machinery persists this blob; a non-serialisable value would
    # fail at park time, long after the tool returned.
    assert json.loads(json.dumps(y.resume_metadata))["source_version"] == 1
