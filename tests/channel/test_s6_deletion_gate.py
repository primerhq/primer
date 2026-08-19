"""S6 P5 exit gate: chat vocabulary is gone from channel/ and trigger/.

Spec: docs/superpowers/ux-revamp/10-s6-design.md sections 7 and 8.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.common.optional import EXTRA_MODULES

REPO = Path(__file__).resolve().parents[2]
# [CROSSPLAN 2026-08-16, F17, F47] the CHAT claim lane's homes are in
# scope too: Task 27 deletes it, and the pre-S6 gate could not see the
# dangling engine_handlers -> chat_dispatcher import.
TREES = [
    REPO / "primer" / "channel",
    REPO / "primer" / "trigger",
    REPO / "primer" / "worker",
    REPO / "primer" / "claim",
    REPO / "primer" / "int",
    REPO / "primer" / "bus",
]

BANNED = [
    "ChatChannelRouter",
    "ChatResponseInbox",
    "ChatChannelDispatcher",
    "chat_dispatcher",
    "chat_inbox",
    "chat_router",
    "chat_message",
    "start_chat",
    "ACTIVE_CHAT_ANCHOR",
    "allow_agent_switch",
    "ClaimKind.CHAT",
    "run_engine_chat",
    "ChatClaimAdapter",
    "chat_tick_router",
]

# Import-shaped only, scanned over the whole package: a ported module may
# still cite "primer/chat/executor.py" in a docstring as provenance
# (primer/session/title.py:1), which is history, not a live edge.
BANNED_IMPORTS = [
    "from primer.chat",
    "import primer.chat",
    "primer.model.chats",
    "ChatTickRouter",
]


def _sources() -> list[Path]:
    out: list[Path] = []
    for tree in TREES:
        out.extend(
            p for p in tree.rglob("*.py")
            if "__pycache__" not in p.parts
        )
    return out


def _all_sources() -> list[Path]:
    return [
        p for p in (REPO / "primer").rglob("*.py")
        if "__pycache__" not in p.parts
    ]


@pytest.mark.parametrize("token", BANNED)
def test_no_chat_vocabulary_survives(token):
    offenders = [
        str(p.relative_to(REPO))
        for p in _sources()
        if token in p.read_text()
    ]
    assert not offenders, f"{token!r} still present in: {offenders}"


@pytest.mark.parametrize("token", BANNED_IMPORTS)
def test_nothing_in_primer_imports_the_chat_engine(token):
    offenders = [
        str(p.relative_to(REPO))
        for p in _all_sources()
        if token in p.read_text()
    ]
    assert not offenders, f"{token!r} still present in: {offenders}"


def test_channels_extra_module_set_is_unchanged():
    assert EXTRA_MODULES["channels"] == (
        "slack_bolt", "telegram", "discord",
    )
