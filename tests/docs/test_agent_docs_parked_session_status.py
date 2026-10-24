"""The agent docs do not say a yield sets a session WAITING (lead ruling 5 of ticket 01a16d72, from #896's source corrections).

A park writes the park columns (``parked_status``, ``parked_event_key``, ...) and leaves ``status`` alone, so a parked session reads
``RUNNING`` (``primer/claim/adapters/sessions.py``, the park branch of ``on_release``; the state diagram in ``docs/dev/subsystems/sessions.md``).
``docs/agents/sessions.md`` said "WAITING - parked on an external event (yield). Set when parked_status=parked", that the two were redundant views
of one fact, and showed ``get_workspace_session`` answering park fields it does not carry.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from primer.claim.adapters.sessions import SessionClaimAdapter

ROOT = Path(__file__).resolve().parents[2]
SESSIONS = (ROOT / "docs/agents/sessions.md").read_text(encoding="utf-8")
YIELDING = (ROOT / "docs/agents/yielding.md").read_text(encoding="utf-8")


def test_the_park_write_leaves_status_alone() -> None:
    """The fact the docs describe: the park branch writes the park columns and no ``status``."""
    source = inspect.getsource(SessionClaimAdapter.on_release)
    park_branch = source.split("if outcome.park is not None:", 1)[1].split("return", 1)[0]
    assert '"parked_status": "parked"' in park_branch
    assert '"status"' not in park_branch


def test_the_sessions_doc_does_not_say_a_yield_sets_waiting() -> None:
    flat = " ".join(SESSIONS.split())
    for claim in (
        "`WAITING` - parked on an external event",
        'Set when `parked_status="parked"`',
        "are redundant surface views of the same underlying fact",
        "yields are how sessions transition to WAITING",
        "`RUNNING ↔ WAITING` - yield enters",
    ):
        assert claim not in flat, claim
    assert re.search(r"A yield .{0,200}does NOT change `status`: the session stays `RUNNING` with `parked_status=\"parked\"`", flat)


def test_no_agent_doc_reads_the_park_from_get_workspace_session() -> None:
    """``get_workspace_session`` answers ``{info, status}`` with no park fields: the docs send the reader to ``find_sessions``."""
    for name, text in (("sessions.md", SESSIONS), ("yielding.md", YIELDING)):
        flat = " ".join(text.split())
        assert "call `workspaces::get_workspace_session(id=...)` and read its status" not in flat, name
        assert 'Returns `status="waiting"`, `parked_status="parked"`' not in flat, name
    assert "`workspaces::find_sessions`" in " ".join(YIELDING.split())
