"""Seglog Task 14 (PR B): append-only writes and segmentation are ON by default, and the flags stay as kill switches."""

from __future__ import annotations

import shutil

import pytest

from primer.model.scheduler import WorkerConfig
from primer.workspace.message_log import append_only_default, apply_worker_config, set_append_only_default
from primer.workspace.segments import DEFAULT_SEGMENT_BYTES, ordered, segment_bytes_default, set_segment_bytes_default
from tests._support.off_golden import ScriptedLLM, assistant_message, make_executor, open_session, user_message


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch):
    """The state of a process that never applied a configuration, with no environment override."""
    monkeypatch.delenv("PRIMER_SESSION_LOG_APPEND_ONLY", raising=False)
    monkeypatch.delenv("PRIMER_SESSION_LOG_SEGMENT_BYTES", raising=False)
    set_append_only_default(None)
    set_segment_bytes_default(None)
    yield
    set_append_only_default(None)
    set_segment_bytes_default(None)


def test_defaults_are_on():
    config = WorkerConfig()
    assert config.session_log_append_only is True and config.session_log_segment_bytes == 1_048_576 == DEFAULT_SEGMENT_BYTES
    assert append_only_default() is True and segment_bytes_default() == 1_048_576, "a process that was never configured (the API without a worker) follows the defaults"


@pytest.mark.parametrize("value, want", [("0", False), ("false", False), ("False", False), ("1", True)])
def test_the_append_only_environment_switch(monkeypatch, value, want):
    monkeypatch.setenv("PRIMER_SESSION_LOG_APPEND_ONLY", value)
    assert append_only_default() is want


@pytest.mark.parametrize("value, want", [("0", 0), ("4096", 4096), ("junk", DEFAULT_SEGMENT_BYTES)])
def test_the_segment_size_environment_switch(monkeypatch, value, want):
    monkeypatch.setenv("PRIMER_SESSION_LOG_SEGMENT_BYTES", value)
    assert segment_bytes_default() == want


def test_the_configuration_is_the_kill_switch(monkeypatch):
    monkeypatch.setenv("PRIMER_SESSION_LOG_APPEND_ONLY", "1")
    monkeypatch.setenv("PRIMER_SESSION_LOG_SEGMENT_BYTES", "512")
    apply_worker_config(WorkerConfig(session_log_append_only=False, session_log_segment_bytes=0))
    assert append_only_default() is False and segment_bytes_default() == 0, "an operator's explicit setting beats the environment"
    apply_worker_config(WorkerConfig(session_log_segment_bytes=2048))
    assert append_only_default() is True and segment_bytes_default() == 2048
    apply_worker_config(WorkerConfig())
    assert segment_bytes_default() == 512, "a configuration that is the default leaves the choice to the environment (how a suite runs segmented)"


def test_the_api_process_applies_the_worker_settings_without_a_worker():
    """An API-only process builds no pool, yet a steer is committed in it: the lifespan applies the same settings."""
    import inspect

    from primer.api import _app_lifespan

    assert "apply_worker_config(config.worker)" in inspect.getsource(_app_lifespan)


@pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available on PATH")
async def test_a_fresh_session_rotates_without_any_configuration(tmp_path):
    backend, workspace, session = await open_session(tmp_path)
    try:
        assert session.session_log_append_only is None and session.append_only() is True
        ex = make_executor(session, ScriptedLLM())
        for n in range(14):
            await ex._persist_turn([user_message(f"q{n} " + "x" * 120_000), assistant_message(f"a{n}")])   # about 1.7 MB in all
        slot = session._state.path / "sessions" / session.session_id  # noqa: SLF001
        names = [n for _, n in ordered(p.name for p in slot.glob("messages*.jsonl"))]
        assert len(names) >= 2, names
        history = await ex._read_messages_jsonl()
        assert [m.parts[0].text[:2] for m in history if m.role == "assistant"] == [f"a{n}"[:2] for n in range(14)]
        assert sum(1 for m in history if m.role == "user") == 15       # the 14 questions and the session's first instruction
    finally:
        await session.aclose()
        await backend.aclose()
