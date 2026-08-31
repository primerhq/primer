"""Workspace locks do not depend on session identity (S1 P5 Task 31).

The mandatory audit before mount deletion. Once a session's binding is
mutable, any lock keyed on WHO is running would be silently released or
orphaned by a switch, so this pins that no such key exists.

Finding: locks are keyed by filesystem path and by scope, never by
session or agent. A session that switches agents keeps every lock it
holds, because the locks were never about the agent.
"""

from pathlib import Path

WS = Path(__file__).resolve().parents[2] / "primer" / "workspace"


def _read(rel: str) -> str:
    return (WS / rel).read_text(encoding="utf-8")


def test_the_lock_module_never_mentions_sessions_or_agents():
    """The audit's headline: identity is simply absent from locking."""
    src = _read("_locks.py").lower()
    assert "session" not in src
    assert "agent_id" not in src


def test_lock_tiers_are_path_and_scope_keyed():
    src = _read("_locks.py")
    assert "_path_locks" in src
    assert "_scope_locks" in src


def test_write_tool_locks_on_the_target_path():
    """str(target) and a scope derived from it: nothing about who
    is writing, only what is being written."""
    src = _read("local/tools/write.py")
    assert "self._locks.hold_write(self._scope_key(target), str(target))" in src


def test_exec_tool_locks_on_declared_write_paths():
    src = _read("local/tools/exec_.py")
    assert "self._locks.hold_paths(resolved)" in src


def test_no_caller_passes_a_session_or_agent_id_as_a_lock_key():
    """Scanning every caller, not just the module, because a key is
    only as scoped as what the caller hands it."""
    for rel in (
        "local/tools/write.py",
        "local/tools/exec_.py",
        "sandbox/fake.py",
    ):
        for line in _read(rel).splitlines():
            if "hold_write(" in line or "hold_paths(" in line:
                lowered = line.lower()
                assert "session" not in lowered, f"{rel}: {line.strip()}"
                assert "agent" not in lowered, f"{rel}: {line.strip()}"
