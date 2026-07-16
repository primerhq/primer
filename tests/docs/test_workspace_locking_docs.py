"""Hygiene checks that the write-safety/locking docs cover the required
concepts: the two lock tiers, the workdir-scoped default vs. strict mode,
the event-loop-safety guarantee, and the exec tool's access/writes hints.

These are content-presence assertions (not exhaustive prose review); the
em-dash ban and link resolution are enforced separately by
``test_docs_hygiene.py`` / ``test_agent_docs_hygiene.py``.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_subsystem_doc_covers_lock_tiers():
    s = (ROOT / "docs/dev/subsystems/workspaces.md").read_text("utf-8")
    for token in ["Tier A", "Tier B", "strict", "workdir", "event loop", "asyncio.Lock"]:
        assert token in s, token
    assert "—" not in s  # no em-dash (U+2014)


def test_agent_doc_covers_access_writes():
    a = (ROOT / "docs/agents/workspaces.md").read_text("utf-8")
    assert "access" in a and "writes" in a
    assert "same directory" in a.lower() or "same-directory" in a.lower()


def test_exec_tool_description_mentions_intent():
    from primer.workspace.local.tools.exec_ import Exec

    assert "access" in Exec.description and "writes" in Exec.description
