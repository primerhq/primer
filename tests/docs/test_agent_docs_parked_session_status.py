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
COOKBOOK = ROOT / "docs/agents/cookbook"
MONITOR = (COOKBOOK / "monitor-and-resume-a-parked-session.md").read_text(encoding="utf-8")
CREATE_RUN = (COOKBOOK / "create-and-run-a-session.md").read_text(encoding="utf-8")
SUMMARISER = (COOKBOOK / "scheduled-summariser.md").read_text(encoding="utf-8")
DEPLOY = (COOKBOOK / "approval-gated-deploy-bot.md").read_text(encoding="utf-8")
DISCORD = (COOKBOOK / "discord-moderation-helper.md").read_text(encoding="utf-8")
GRAPHS = (ROOT / "docs/agents/graphs.md").read_text(encoding="utf-8")


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


# The same false claim (a yield parks a session at status WAITING) stood in the cookbook recipes and
# in the graphs doc too (ticket 01a16d3c, item 5): the park branch of ``on_release`` writes the park
# columns and leaves ``status`` alone, so a parked session reads RUNNING.
_FALSE_CLAIMS = (
    "whose `status` is `waiting` (parked inside its turn",
    "the surface shows `status: \"waiting\"`",
    "reaches `status: \"waiting\"`",
    "`status: \"waiting\"` means the session is parked",
    "a session with `status: \"waiting\"` parked on",
    "`status: \"waiting\"` means the agent yielded",
    "`waiting` means the agent yielded",
    "get_workspace_session returns `status=\"waiting\"`",
    "parks the session WAITING",
)

_FALSE_SHAPES = (
    '"status": "waiting"',
    '"waiting": { "kind"',
)


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_no_agent_doc_copy_says_a_yield_parked_session_reads_waiting() -> None:
    """The copies of the false claim are gone from the cookbook recipes and the graphs doc."""
    for name, text in (
        ("cookbook/monitor-and-resume-a-parked-session.md", MONITOR),
        ("cookbook/create-and-run-a-session.md", CREATE_RUN),
        ("cookbook/scheduled-summariser.md", SUMMARISER),
        ("cookbook/approval-gated-deploy-bot.md", DEPLOY),
        ("cookbook/discord-moderation-helper.md", DISCORD),
        ("graphs.md", GRAPHS),
    ):
        flat = _flat(text)
        for claim in _FALSE_CLAIMS:
            assert claim not in flat, f"{name} still says: {claim}"
        if name.startswith("cookbook/"):
            for shape in _FALSE_SHAPES:
                assert shape not in flat, f"{name} still shows: {shape}"


def test_the_cookbook_and_graphs_docs_point_to_the_row_for_the_park() -> None:
    """The replacement each page carries: a park keeps status running; the park fields are the row's."""
    monitor = _flat(MONITOR)
    assert "A park does not move `status`" in monitor
    assert "carries no park field" in monitor
    assert "workspaces::find_sessions" in monitor
    assert "parked_event_key" in monitor
    assert "workspaces::list_my_sessions" in monitor
    assert "a park keeps `status` `running` and sets `parked_status`" in _flat(CREATE_RUN)
    assert "a park keeps `status` and sets `parked_status`" in _flat(SUMMARISER)
    assert "it reads `status: \"running\"` with `parked_status` set" in _flat(DEPLOY)
    assert "it reads `status: \"running\"` with `parked_status` set" in _flat(DISCORD)
    assert "it keeps `status=\"running\"` with `parked_status` set" in _flat(GRAPHS)
