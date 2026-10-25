"""The console mirrors the rank rule of the binding switch on the session doc's binding chip (G3-01 / ticket 01a16a35-a788).

A viewer who ranks below the session's creator (and it is not their own session) gets the binding switch disabled with a reason: the server
refuses the same way (403 forbidden_rank, the steer rule, decided before anything is written), so this only removes the dead click. The rule
is the one of ``primer.authz.may_drive``: an admin ranks with the system principal (never blocked), a row with no recorded creator ranks as
an ordinary user, and one's own session (type and id on ``initiated_by``) is never blocked. ``NV_bindingBlockedReason`` fails open (``null``)
when it cannot decide (no principal served, unknown role), leaving the server as the decider. The chip reads the viewer's principal from the
shell's cached ``/auth/status`` read (one fetch, not two) and the server's refusal toast remains the backstop.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")


def _blocked(session: dict, principal: dict | None) -> str | None:
    from py_mini_racer import MiniRacer

    start = SRC.index("function NV_bindingRoleRank(")
    end = SRC.index("\n}\n", SRC.index("function NV_bindingBlockedReason(", start)) + 3
    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(SRC[start:end])
    return json.loads(
        ctx.eval(f"JSON.stringify(NV_bindingBlockedReason({json.dumps(session)}, {json.dumps(principal)}))")
    )


def _session(creator: dict | None) -> dict:
    return {"initiated_by": creator}


def _principal(role, pid: str = "u-me", ptype: str = "user") -> dict:
    return {"type": ptype, "id": pid, "display": "me", "role": role, "source": "local"}


def test_a_user_below_an_admins_session_is_blocked_with_the_reason():
    creator = {"type": "user", "id": "u-root", "display": "root", "role": "admin", "source": "local"}
    reason = _blocked(_session(creator), _principal("user"))
    assert reason is not None and "rank below" in reason and "switch" in reason, reason


def test_an_admin_is_never_blocked():
    creator = {"type": "user", "id": "u-root", "display": "root", "role": "admin", "source": "local"}
    assert _blocked(_session(creator), _principal("admin")) is None


def test_a_viewer_at_the_creators_rank_is_not_blocked():
    creator = {"type": "user", "id": "u-root", "display": "root", "role": "user", "source": "local"}
    assert _blocked(_session(creator), _principal("user")) is None


def test_ones_own_session_is_never_blocked_even_below_the_recorded_role():
    """A demoted admin (a viewer now at user rank) on their own session recorded while they were an admin: the id match keeps the chip on,
    as the server's own-session rule (may_drive own_session) does."""
    creator = {"type": "user", "id": "u-me", "display": "me", "role": "admin", "source": "local"}
    assert _blocked(_session(creator), _principal("user")) is None


def test_a_reduced_creator_that_is_not_their_own_falls_back_to_the_rank():
    creator = {"display": "root", "role": "admin"}  # no id: a reduced view, compared by rank
    assert _blocked(_session(creator), _principal("user")) is not None


def test_a_session_with_no_recorded_creator_ranks_as_an_ordinary_user():
    """A row with no recorded creator ranks as an ordinary user: a restricted viewer is below it, a user is not."""
    assert _blocked(_session(None), _principal("restricted")) is not None
    assert _blocked(_session(None), _principal("user")) is None


def test_without_a_served_principal_the_chip_is_left_to_the_server():
    """Fail open: no principal served (unauthenticated view, or a principal without a usable role) and the server stays the decider."""
    creator = {"type": "user", "id": "u-root", "display": "root", "role": "admin", "source": "local"}
    assert _blocked(_session(creator), None) is None
    assert _blocked(_session(creator), {"type": "user", "id": "u-me", "role": None}) is None


def test_the_chip_applies_the_rule_and_reads_the_viewers_principal_from_auth_status():
    """Static wiring: the chip computes the reason from the session row and the viewer's principal (the shell's cached ``/auth/status``
    read), disables the chip button with the reason in its title when blocked, and leaves the server's refusal as the backstop."""
    assert "NV_bindingBlockedReason(" in SRC
    assert '"nv-shell:auth-status"' in SRC
    chip = SRC[SRC.index("function NV_BindingChip("):]
    end = chip.index("\nfunction ")
    chip = chip[:end]
    for needle in (
        "var blocked = NV_bindingBlockedReason(props.session, principal)",
        "disabled={!!blocked}",
        'data-testid="nv-binding-chip"',
    ):
        assert needle in chip, needle
    # the reason is the chip's title while blocked, the usual title otherwise
    assert "blocked ||" in chip
