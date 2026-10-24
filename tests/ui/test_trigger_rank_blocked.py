"""The console mirrors the rank rule of a manual fire on the trigger buttons (tickets 01a16e65-12d1 and 01a16ee4).

A viewer who ranks below a trigger's owner (and it is not their own) gets Fire now, Edit and Delete disabled with a reason: the server refuses
the same way (403 forbidden_rank over REST, type=forbidden from a tool), so this only removes the dead click. The rule is the one of
``primer.trigger.owner`` (``refuse_fire_now`` / ``refuse_trigger_change``): an admin ranks with the system principal (never blocked), a row
with no recorded owner ranks as an ordinary user, and one's own trigger is never blocked. A non-manager's view reduces the owner to
``{display, role}`` (no id), so ownership is compared by the id only when the full owner is served (a manager's own view, which always
includes one's own trigger); a reduced owner that cannot be matched is compared by rank. ``TR_rankBlockedReason`` is the one rule of the
three buttons and fails open (``null``) when it cannot decide (no principal served), leaving the server as the decider. It is mirrored on the
list row and the detail page.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "triggers.jsx").read_text(encoding="utf-8")


def _blocked(owner, principal, verb: str = "fire") -> str | None:
    from py_mini_racer import MiniRacer

    start = SRC.index("function TR_roleRank(")
    end = SRC.index("\n}\n", SRC.index("function TR_rankBlockedReason(", start)) + 3
    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(SRC[start:end])
    return json.loads(
        ctx.eval(f"JSON.stringify(TR_rankBlockedReason({json.dumps({'owner': owner})}, {json.dumps(principal)}, {json.dumps(verb)}))")
    )


def _principal(role, pid: str = "u-me", ptype: str = "user") -> dict:
    return {"type": ptype, "id": pid, "display": "me", "role": role, "source": "local"}


def test_a_user_below_an_admin_owner_is_blocked_on_every_button_with_the_reason():
    owner = {"type": "user", "id": "u-root", "display": "root", "role": "admin"}
    for verb in ("fire", "edit", "delete"):
        reason = _blocked(owner, _principal("user"), verb)
        assert reason is not None and "rank below" in reason and verb in reason, (verb, reason)


def test_an_admin_is_never_blocked():
    owner = {"type": "user", "id": "u-root", "display": "root", "role": "admin"}
    assert _blocked(owner, _principal("admin"), "fire") is None


def test_a_viewer_at_the_owner_rank_is_not_blocked():
    assert _blocked({"display": "root", "role": "user"}, _principal("user"), "fire") is None


def test_ones_own_trigger_is_never_blocked_even_below_the_recorded_role():
    """A demoted admin (a viewer now at user rank) on their own trigger recorded while they were an admin: the full owner is served (they
    manage it), and the id match keeps the buttons on, as the server's same-owner rule does."""
    owner = {"type": "user", "id": "u-me", "display": "me", "role": "admin"}
    assert _blocked(owner, _principal("user"), "delete") is None


def test_a_reduced_owner_that_is_not_their_own_falls_back_to_the_rank():
    owner = {"display": "me", "role": "admin"}  # same display, no id: the view of a non-manager, compared by rank
    assert _blocked(owner, _principal("user"), "fire") is not None


def test_a_user_below_a_system_owner_is_blocked_and_an_admin_is_not():
    owner = {"type": "system", "id": "system", "display": "system", "role": None}
    assert _blocked(owner, _principal("user"), "fire") is not None
    assert _blocked(owner, _principal("admin"), "fire") is None


def test_a_restricted_viewer_is_blocked_by_a_user_owner_and_by_an_ownerless_trigger():
    """A row with no recorded owner ranks as an ordinary user; restricted is below it."""
    assert _blocked(None, _principal("restricted"), "edit") is not None
    assert _blocked({"display": "root", "role": "user"}, _principal("restricted"), "edit") is not None
    assert _blocked(None, _principal("user"), "edit") is None


def test_without_a_served_principal_the_buttons_are_left_to_the_server():
    owner = {"display": "root", "role": "admin"}
    assert _blocked(owner, None, "fire") is None
    assert _blocked(owner, {"type": "user", "id": "u-me", "role": None}, "fire") is None


def test_the_row_and_the_detail_page_apply_the_rule_to_fire_edit_and_delete():
    """Static wiring: the rule is computed for the three verbs on both surfaces, the buttons take it into their ``disabled`` / ``title``, and
    both surfaces read the viewer's principal from ``/auth/status``."""
    for needle in (
        'fire: TR_rankBlockedReason(trigger, principal, "fire")',
        'edit: TR_rankBlockedReason(trigger, principal, "edit")',
        'del: TR_rankBlockedReason(trigger, principal, "delete")',
    ):
        assert needle in SRC, needle
    for needle in (
        'fire: TR_rankBlockedReason(t, principal, "fire")',
        'edit: TR_rankBlockedReason(t, principal, "edit")',
        'del: TR_rankBlockedReason(t, principal, "delete")',
    ):
        assert needle in SRC, needle
    row = SRC.index("function TR_TriggerRow(")
    detail = SRC.index("function TR_TriggerDetail(")
    row_src = SRC[row:detail]
    for needle in ("disabled={busy || !!blocked.fire}", "disabled={busy || !!blocked.edit}", "disabled={busy || !!blocked.del}"):
        assert needle in row_src, needle
    detail_src = SRC[detail:]
    for needle in ("disabled={fireBusy || !!blocked.fire}", "disabled={!!blocked.edit}", "disabled={!!blocked.del}"):
        assert needle in detail_src, needle
    assert SRC.count('"/auth/status"') >= 2
