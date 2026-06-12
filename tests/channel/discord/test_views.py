"""Offline tests for the Discord UI classes."""

from __future__ import annotations

import re

import pytest

discord = pytest.importorskip("discord")
from primer.channel.discord.views import (
    REJECT_MODAL_CUSTOM_ID_PREFIX,
    AgentSelectView,
    ApprovalView,
    build_approval_custom_ids,
    decode_custom_id,
)


def test_agent_select_view_builds_with_options():
    async def _on_pick(interaction, agent_id):  # noqa: ANN001
        return None

    view = AgentSelectView(
        options=[
            {"agent_id": "a", "label": "A"},
            {"agent_id": "b", "label": "B"},
        ],
        on_pick=_on_pick,
    )
    select = view.children[0]
    assert len(select.options) == 2
    assert {o.value for o in select.options} == {"a", "b"}


def test_build_custom_ids_are_under_100_chars():
    a, r = build_approval_custom_ids(ws="ws1", sid="s1", tcid="tc1")
    assert a == "approve:ws1:s1:tc1"
    assert r == "reject:ws1:s1:tc1"
    assert len(a) < 100 and len(r) < 100


def test_decode_custom_id_parses_verb_and_ids():
    parsed = decode_custom_id("approve:wsA:sB:tcC")
    assert parsed == ("approve", "wsA", "sB", "tcC")
    parsed = decode_custom_id("reject:wsA:sB:tcC")
    assert parsed == ("reject", "wsA", "sB", "tcC")


def test_decode_custom_id_handles_modal_prefix():
    parsed = decode_custom_id(f"{REJECT_MODAL_CUSTOM_ID_PREFIX}:wsA:sB:tcC")
    assert parsed == (REJECT_MODAL_CUSTOM_ID_PREFIX, "wsA", "sB", "tcC")


def test_approval_view_has_two_buttons_with_correct_custom_ids():
    view = ApprovalView(ws="ws", sid="s", tcid="tc")
    cids = [c.custom_id for c in view.children]
    assert "approve:ws:s:tc" in cids
    assert "reject:ws:s:tc" in cids


def test_approval_view_has_no_timeout():
    view = ApprovalView(ws="ws", sid="s", tcid="tc")
    assert view.timeout is None
