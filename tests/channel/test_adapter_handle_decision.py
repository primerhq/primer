"""A restricted approval that a chat user cannot decide is reported back, not raised (ticket 01a11b6c, follow-up of 01a11b64).

``ChannelInbox.handle_response`` raises ``ApproverRefusedError`` for a ``tool_approval`` reply on a gate routed to specific approvers (a
chat-platform user is an unidentified decider). The adapters' interactive handlers each ran their post-decision step (Slack's and
Telegram's message edit, Discord's "Approved" edit and modal acknowledgement) around a relay that could raise, so they skipped it silently
(Slack, Telegram) or had already claimed an approval that was refused (Discord). ``ChannelAdapter._handle_decision`` now answers whether the
decision was accepted, so every handler can tell the clicker where to decide it.
"""

from __future__ import annotations

import pytest

from primer.channel import adapter as adapter_module
from primer.channel.null_adapter import NullChannelAdapter
from primer.session.approvers import ApproverRefusedError


class _Inbox:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.envelopes: list = []

    async def handle_response(self, env) -> None:
        self.envelopes.append(env)
        if self.error is not None:
            raise self.error


class _Adapter(NullChannelAdapter):
    def __init__(self, inbox: _Inbox) -> None:
        super().__init__()
        self._inbox = inbox


async def _decide(adapter: _Adapter):
    return await adapter._handle_decision(
        workspace_id="ws", session_id="s", tool_call_id="tc", decision="approved", reason=None, user_id="U1",
    )


async def test_an_accepted_decision_reports_true() -> None:
    inbox = _Inbox()

    assert await _decide(_Adapter(inbox)) is True
    assert len(inbox.envelopes) == 1


async def test_a_decision_the_gate_refuses_reports_false_instead_of_raising() -> None:
    inbox = _Inbox(ApproverRefusedError("routed to specific approvers"))

    assert await _decide(_Adapter(inbox)) is False


async def test_any_other_failure_still_raises() -> None:
    with pytest.raises(RuntimeError, match="bus down"):
        await _decide(_Adapter(_Inbox(RuntimeError("bus down"))))


def test_the_notice_says_where_to_decide_it() -> None:
    notice = adapter_module.APPROVAL_ROUTED_NOTICE
    assert "console" in notice and "routed to specific approvers" in notice
    assert len(notice) <= 200, "Telegram's alert on a callback query takes no more than 200 characters"
