"""UI e2e: a subagent's failure card has no Retry, while the session's own failed turn does (follow-up to the Retry PR, console review C-024).

``SH_retryInstruction`` knows nothing of depth: it offers the operator's instruction again for any error row that ends the transcript. The card keeps a SUBAGENT's failure from getting one with
``depth ? null : SH_retryInstruction(...)``. The seeded transcript here ends on a delegated run's fatal error (a real one: ``tests/ui_e2e/_delegation_seed.py``) followed by the session's own fatal
error, so on the page BOTH rows pass every condition the pure decision checks. Without the guard there would be two Retry buttons; with it there is one, on the session's own card.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import expect

from tests.ui_e2e import _delegation_seed as seed
from tests.ui_e2e._studio_helpers import open_session_in_studio

OWN_FAILURE = "the session's own turn: the model fell over"


@pytest.mark.ui_e2e
def test_a_subagents_failure_card_has_no_retry_and_the_sessions_own_failure_has_one(console_url, page, tmp_path, delegation_session) -> None:
    wid, sid = delegation_session.wid, delegation_session.sid
    seeded = seed.build(failures=True)
    records = [r for r in seeded.records if r["seq"] <= seeded.delegated_failure_seq]
    assert records[0]["kind"] == "user_input", "the transcript opens with a plain instruction, so a Retry has something to resend"
    own = {"seq": records[-1]["seq"] + 1, "kind": "error", "created_at": "2026-10-06T12:00:06Z", "node_id": None,
           "payload": {"message": OWN_FAILURE, "code": "server_error", "fatal": True}}
    records.append(own)
    log = tmp_path / wid / ".state" / "sessions" / sid / "messages.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    open_session_in_studio(page, console_url, wid, sid, kind="agent")

    nested = page.get_by_test_id(f"nv-subagent-failure:{seeded.delegated_failure_seq}")
    expect(nested).to_be_visible(timeout=20_000)
    expect(nested.locator(".nv-turn-error")).to_contain_text(seed.GRANDCHILD_FAILURE)
    expect(nested.locator('[data-testid^="nv-turn-retry:"]')).to_have_count(0)

    # the control: the session's own failed turn, drawn on the same page, does offer it
    own_card = page.get_by_test_id(f"nv-turn:{own['seq']}")
    expect(own_card).to_contain_text(OWN_FAILURE)
    expect(own_card.locator('[data-testid^="nv-turn-retry:"]')).to_have_count(1)

    # and nothing else on the page has one
    expect(page.locator('[data-testid^="nv-turn-retry:"]')).to_have_count(1)
