"""G11-06/07/08 (audit /var/tmp/revamp/audit/G11.md): the channels onboarding
gaps and the webhook-token doc wording.

G11-07: the channel row's trash button deletes in ONE click (no confirm,
while the provider delete two navigation levels away gets the house
confirmDialog gate), and the delete's onError special-cases a 409 "workspace
associations still reference this channel" - dead code: the channel router
(primer/api/routers/channels.py make_channel_router) carries no reference
check, so the route never answers that 409 (only the ChannelProvider router
has the cascade-block ReferenceCheck).

G11-08: nothing tells the operator that steps 3-4 of
zero-to-working-channel (enable chats, add a rule) are missing: the
just-created channel's screen names no next step, and a chats-enabled room
with no rule simply never starts a session while no surface says "no rule".

G11-06: docs/dev/subsystems/triggers.md says the webhook token is "never
returned after initial creation response", but view_for (primer/trigger/
owner.py) serves the FULL token to the trigger's owner / an admin / system
on every read, masked for everyone else.

Source scans scoped to the real components, plus MiniRacer over the
extracted no-rule-room helper (scalar returns).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "channels.jsx").read_text(encoding="utf-8")
RULES = (ROOT / "ui" / "components" / "channel_rules.jsx").read_text(encoding="utf-8")
DOC = (ROOT / "docs" / "dev" / "subsystems" / "triggers.md").read_text(encoding="utf-8")


def _no_rule_rooms_src() -> str:
    """The source of the top-level function CR_noRuleRooms (brace-matched)."""
    prefix = "function CR_noRuleRooms("
    i = RULES.index(prefix)
    k = i + len(prefix) - 1
    depth = 0
    while True:
        if RULES[k] == "(":
            depth += 1
        elif RULES[k] == ")":
            depth -= 1
            if depth == 0:
                break
        k += 1
    brace = RULES.index("{", k)
    depth = 0
    for k in range(brace, len(RULES)):
        if RULES[k] == "{":
            depth += 1
        elif RULES[k] == "}":
            depth -= 1
            if depth == 0:
                return RULES[i:k + 1]
    raise AssertionError("unbalanced braces in CR_noRuleRooms")


# --- G11-07: the channel delete ---------------------------------------------


def test_the_channel_row_delete_is_confirm_gated() -> None:
    i = SRC.index('title="Delete channel"')
    block = SRC[i:SRC.index("</button>", i)]
    assert "del.mutate" not in block, (
        "the channel row's trash still fires del.mutate in one click; it must "
        "go through the house confirm gate first (the provider delete's pattern)"
    )
    assert 'confirmLabel: "Delete channel"' in SRC, (
        "no confirmDialog gate for the channel delete (confirmLabel missing)"
    )


def test_the_dead_409_workspace_associations_copy_is_gone() -> None:
    # The channel router (make_channel_router) has no reference check: only
    # the ChannelProvider router carries the cascade-block ReferenceCheck, so
    # DELETE /channels/{id} never answers the 409 this copy described.
    assert "workspace associations still reference this channel" not in SRC


# --- G11-08: the zero-to-working-channel next steps --------------------------


def test_the_just_created_channel_names_the_rule_step() -> None:
    assert "Next: add a rule" in SRC, (
        "the just-created channel's screen must name the missing-rule next step"
    )
    assert 'navigate("/channels/rules")' in SRC, (
        "the next-step affordance must carry the house navigation to the "
        "Channel-rules page (the router shim path /channels/rules)"
    )
    # The created row must reach the page so the screen can name THAT channel.
    modal = SRC[SRC.index("function NewChannelModal("):]
    assert re.search(r"onSuccess:\s*\(\s*row\s*\)\s*=>\s*onCreated\(row\)", modal), (
        "the create success must hand the created row to the page"
    )


def test_the_rules_page_flags_a_chats_enabled_channel_without_a_rule() -> None:
    assert "no rule yet" in RULES, "the rules page has no no-rule marker"
    assert re.search(r"CR_noRuleRooms\(", RULES), (
        "the rules page must compute the chats-enabled channels with no channel trigger"
    )
    ids = _flagged_room_ids()
    assert ids == ["ch-on"], ids


def _flagged_room_ids() -> list:
    from py_mini_racer import MiniRacer

    rows = [
        {"id": "ch-on", "provider_id": "p1", "config": {"chats": {"enabled": True}}},
        {"id": "ch-off", "provider_id": "p1", "config": {"chats": {"enabled": False}}},
        {"id": "ch-nocfg", "provider_id": "p2", "config": {}},
        {"id": "ch-r", "provider_id": "p1", "config": {"chats": {"enabled": True}}},
    ]
    trigs = [
        {"id": "t1", "config": {"kind": "channel", "provider_id": "p2", "channel_id": "ch-nocfg"}},
        {"id": "t2", "config": {"kind": "webhook"}},
        {"id": "t3", "config": {"kind": "channel", "provider_id": "p1", "channel_id": "ch-r"}},
    ]
    ctx = MiniRacer()
    ctx.eval(_no_rule_rooms_src())
    out = ctx.eval(
        "JSON.stringify(CR_noRuleRooms("
        + json.dumps(rows)
        + ", "
        + json.dumps(trigs)
        + ").map(function (c) { return c.id; }))"
    )
    ctx.close()
    return json.loads(out)


# --- G11-06: the webhook token doc line --------------------------------------


def test_the_token_line_is_qualified_to_owner_reads() -> None:
    lines = [l for l in DOC.splitlines() if "WebhookTriggerConfig" in l and "token" in l]
    assert lines, "the WebhookTriggerConfig token line is gone"
    line = lines[0]
    assert "never returned after initial creation response" not in line, (
        "the absolute 'never returned' phrasing still stands unqualified"
    )
    assert "owner" in line and "admin" in line, (
        "the line must say the token is served in full on owner/admin reads"
    )
    assert "masked" in line, "the line must say it is masked to everyone else"
    assert "\u2014" not in line, "docs/dev is em-dash-enforced"
