"""The console mirrors the server's access levels on the trigger buttons (ACL-P3-T7).

The rank rule is gone: the trigger backend (Task 4) enforces access levels
(read sees, full edits, fires and manages subscriptions, owner deletes,
rotates the token and shares), and the detail page reads the viewer's level
from ``GET /triggers/{id}/access``. ``TR_writeGates(your_level)`` is the one
rule of the buttons, the same shape as the collection's ``KN_writeGates``:
at ``read`` Fire now, Edit and the subscription writes stay drawn but
disabled with "Read-only: you can view this trigger"; at ``full`` Delete,
token rotation and Share are disabled with "Only an owner can do this"; at
``owner`` everything is live. A level that is still unknown (the access read
in flight) disables nothing - the server enforces the write. The list row no
longer mirrors any rule: it always offers Fire now/Edit/Delete and says a
refusal on the spot (the row's toasts, pinned in
test_trigger_row_actions_report_their_errors.py).

``TR_writeGates`` runs in MiniRacer against the real source; the wiring is
source-checked against the real lines.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
TRIGGERS = ROOT / "ui" / "components" / "triggers.jsx"
SRC = TRIGGERS.read_text(encoding="utf-8")

RO_NOTE = "Read-only: you can view this trigger"
OWNER_NOTE = "Only an owner can do this"

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _mini_ctx(src: str):
    ctx = mini_react_context(src)
    _OPEN_CONTEXTS.append(ctx)
    return ctx


def _around(anchor: str, before: int = 420, after: int = 140) -> str:
    i = SRC.index(anchor)
    return SRC[max(0, i - before) : i + after]


# ===========================================================================
# The level decision
# ===========================================================================


def test_the_level_gates_are_the_single_decision() -> None:
    """TR_writeGates against the real source: read disables the fire/edit/
    subscription writes; full loses the delete/rotate/share to the owner;
    owner keeps them; an unknown level (the access read in flight) disables
    nothing - the server enforces the save."""
    ctx = _mini_ctx(transpile(TRIGGERS))

    def gates(level) -> dict:
        return json.loads(
            ctx.eval(f"JSON.stringify(TR_writeGates({json.dumps(level)}))")
        )

    assert gates(None) == {"ro": False, "ownerOnly": False}
    assert gates("read") == {"ro": True, "ownerOnly": False}
    assert gates("full") == {"ro": False, "ownerOnly": True}
    assert gates("owner") == {"ro": False, "ownerOnly": False}


# ===========================================================================
# The wiring: the level comes from the access sub-resource
# ===========================================================================


def test_the_detail_reads_its_level_from_the_access_subresource() -> None:
    """One read per open trigger (useResource, key trigger-access:<id>) from
    the access sub-resource; the level is the view's your_level."""
    read = _around('"trigger-access:"', before=0, after=500)
    assert "/access" in read, "the level must come from GET /triggers/{id}/access"
    assert "access.data?.your_level" in SRC, "the level is the view's your_level"


def test_the_rank_rule_and_its_principal_read_are_gone() -> None:
    """The dead rank rule: the helpers are removed and neither surface reads
    /auth/status for it anymore (the server now decides by level)."""
    assert "TR_rankBlockedReason" not in SRC
    assert "TR_roleRank" not in SRC
    assert '"/auth/status"' not in SRC
    assert "blocked.fire" not in SRC
    assert "blocked.edit" not in SRC
    assert "blocked.del" not in SRC


# ===========================================================================
# The buttons take the gates
# ===========================================================================


def test_the_detail_buttons_take_the_gates() -> None:
    """Drawn at every level, disabled with the reason (never hidden
    silently): Fire now and Edit at read; Delete, Share, the token rotation
    and the HMAC set/clear at read and at full; the notes are the disabled
    controls' words."""
    assert "disabled={fireBusy || gates.ro}" in SRC, "Fire now is not read-gated"
    assert "disabled={gates.ro}" in SRC, "Edit is not read-gated"
    assert "disabled={gates.ro || gates.ownerOnly}" in SRC, "the owner writes are not gated"
    assert "disabled={rotateBusy || gates.ro || gates.ownerOnly}" in SRC, "the rotation is not owner-gated"
    for tid in ("set-hmac-btn", "clear-hmac-btn", "trigger-share", "rotate-token-btn"):
        assert "gates.ro || gates.ownerOnly" in _around(f'data-testid="{tid}"'), f"{tid} is not gated"
    assert RO_NOTE in SRC, "the read-only reason is missing"
    assert OWNER_NOTE in SRC, "the owner-only reason is missing"


def test_the_subscription_writes_are_read_gated() -> None:
    """Add/Edit/Delete subscription and the row's parallelism/enabled
    controls: drawn at read, disabled with the read-only reason; the detail
    threads its read gate to the panel."""
    panel = SRC[SRC.index("function TR_SubscriptionsPanel(") : SRC.index("function TR_TriggerDetail(")]
    assert "disabled={rowBusy || ro}" in panel, "the subscription controls are not read-gated"
    detail = SRC[SRC.index("function TR_TriggerDetail(") :]
    assert "ro={gates.ro}" in detail, "the panel does not receive the read gate"


def test_the_row_always_offers_its_actions_and_leaves_the_refusal_to_the_toast() -> None:
    """The list row mirrors no rule anymore: busy-gated only, no principal
    prop - a refusal is said on the spot by the row's toasts (the server is
    the decider, as in the rank era)."""
    row = SRC[SRC.index("function TR_TriggerRow(") : SRC.index("function TR_TriggerDetail(")]
    assert ", principal" not in row, "the row still takes the principal"
    assert "disabled={busy}" in row, "the row's buttons are not busy-gated only"
