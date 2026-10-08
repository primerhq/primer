"""The channel form and the triggers page use the console's own vocabulary (ADM-19 of the 2026-10-08 admin review).

The channel form had a section "Chats config" with a toggle "Chats enabled / allow inbound chat messages on this channel", and the empty Triggers page said a trigger dispatches to "chat
messages", where the rest of the console says SESSIONS: two names for the same thing on adjacent screens. What the setting does is documented in ``docs/agents/channels.md``: incoming messages
in the room start primer sessions, and the agent they run under comes from the channel trigger's binding, not from the room. The config key ``chats.enabled`` is a code name the docs defend
("named for the platform it faces rather than for a primer entity"), so it stays: in the request body, in the test id (``channel-chats-enabled``, which the journeys click) and, for whoever
needs it, in a tooltip.

For the empty Triggers page: the four things a trigger subscription can do are the model's ``SubscriptionKind`` (fresh agent session, fresh graph session, a session waiting on the trigger,
and appending to an existing session), and the subscription dialog describes them as "Start a fresh workspace session bound to an agent." and "Steer an existing session with the rendered
payload." The sentence uses those words.

These are visible strings in JSX, so they are source checks (this checkout has no render harness for the pages); ``tests/ui_e2e/test_channels_onboarding_journey.py`` asserts them on the real
form.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHANNELS = (ROOT / "ui" / "components" / "channels.jsx").read_text(encoding="utf-8")
TRIGGERS = (ROOT / "ui" / "components" / "triggers.jsx").read_text(encoding="utf-8")


def _channel_form() -> str:
    start = CHANNELS.index('<CH_Toggle\n            checked={chatsEnabled}')
    return CHANNELS[CHANNELS.rindex("<div style={{ borderTop", 0, start):CHANNELS.index("</Modal>", start)]


def test_the_channel_form_names_the_section_and_the_toggle_in_the_consoles_words() -> None:
    form = _channel_form()

    assert ">Inbound messages</div>" in form
    assert 'label="Start sessions from inbound messages"' in form
    help_text = re.search(r'help="([^"]*)"', form)
    assert help_text, "the toggle has its help line"
    assert "start sessions" in help_text.group(1) and "trigger" in help_text.group(1), "it says what happens and where the agent comes from"


def test_the_retired_words_are_gone_from_what_the_channel_form_shows() -> None:
    form = _channel_form()
    visible = re.sub(r"\{[^{}]*\}", "", form)

    for retired in ("Chats config", "Chats enabled", "chat messages", "inbound chat"):
        assert retired not in visible, f"{retired!r} is still shown"


def test_the_code_name_stays_where_code_and_tests_read_it_and_in_a_tooltip() -> None:
    form = _channel_form()

    assert 'testid="channel-chats-enabled"' in form, "the journeys click it by this id"
    assert "title=\"config.chats\"" in form or "title='config.chats'" in form, "the key stays one hover away"
    body = CHANNELS[CHANNELS.index("chats: {"):]
    assert "enabled: chatsEnabled," in body[:200], "and in the request body"


def test_the_empty_triggers_page_says_what_a_subscription_does_in_the_subscription_dialogs_words() -> None:
    start = TRIGGERS.index('<div className="head">No triggers configured</div>')
    empty = re.sub(r"\s+", " ", TRIGGERS[start:TRIGGERS.index("</div>", TRIGGERS.index('<div className="sub">', start))])

    assert "chat messages" not in empty
    assert "steer an existing session" in empty
    assert "fresh agent session" in empty and "fresh graph session" in empty
