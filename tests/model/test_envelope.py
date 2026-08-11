"""PromptEnvelope/ResponseEnvelope live in core; channel re-exports them."""

from __future__ import annotations


def test_envelopes_importable_from_core_model() -> None:
    from primer.model.envelope import PromptEnvelope, ResponseEnvelope

    env = PromptEnvelope(
        kind="ask_user",
        workspace_id="ws-1",
        session_id="sess-1",
        tool_call_id="tc-1",
        prompt="pick one",
        response_schema=None,
        choices=["a", "b"],
        timeout_at_iso=None,
    )
    assert env.kind == "ask_user"
    assert ResponseEnvelope is not None


def test_channel_adapter_still_reexports_envelopes() -> None:
    from primer.channel.adapter import PromptEnvelope as ReExported
    from primer.model.envelope import PromptEnvelope

    assert ReExported is PromptEnvelope
