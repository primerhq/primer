"""Tests for PromptEnvelope attribution fields."""

from __future__ import annotations

from primer.channel.adapter import PromptEnvelope


def test_attribution_fields_default_none():
    e = PromptEnvelope(kind="ask_user", workspace_id="ws", session_id="s", tool_call_id="tc",
                       prompt="hi", response_schema=None, choices=None, timeout_at_iso=None)
    assert e.workspace_name is None and e.session_label is None


def test_attribution_fields_set():
    e = PromptEnvelope(kind="ask_user", workspace_id="ws", session_id="s", tool_call_id="tc",
                       prompt="hi", response_schema=None, choices=None, timeout_at_iso=None,
                       workspace_name="Ops", session_label="s-1")
    assert e.workspace_name == "Ops" and e.session_label == "s-1"
