"""UI e2e: a subagent's failure and its recoverable problem are drawn INSIDE the subagent's own block (console review 2026-10-08, ticket 01a11c1e).

``NV_subagentRows`` drew only the rows that carry a ``label``, and an error row carries none (its words are in ``payload.message``), so a subagent that
FAILED showed nothing in its block: the docs said a subagent's failure is drawn inside its own block, and it was not. This seeds a session with a REAL
delegated run (``tests/ui_e2e/_delegation_seed.py``, ``build(failures=True)``: the records come from ``translate_stream_event`` and the real
``DelegationRecorder``).

Asserted on the DOM, by containment:

* the grandchild's failure (a fatal Error) is a red card INSIDE the child's own call block, with the provider's words;
* a second call whose run ends the way the OpenResponses stream does (the agent loop holds the non-fatal Error, yields the Done first and the Error last, then
  raises, and the call is answered with an ERROR result) shows ONE red card in its own block with the provider's words, and no quiet line that says the turn is
  continuing: the notice of a failed call IS the failure;
* those two are the only red cards in the whole transcript: the parent's turn did not fail.
"""

from __future__ import annotations

import json

from playwright.sync_api import expect

from tests.ui_e2e import _delegation_seed as seed
from tests.ui_e2e._studio_helpers import open_session_in_studio
from tests.ui_e2e.test_delegated_run_nests_in_its_call_journey import _seed_session


def test_a_subagents_failure_renders_inside_its_block_and_a_failed_calls_notice_is_the_failure(base_url, console_url, page, tmp_path, unique_suffix) -> None:
    wid, sid = _seed_session(base_url, tmp_path, unique_suffix)
    seeded = seed.build(failures=True)
    assert seeded.delegated_failure_seq and seeded.delegated_notice_seq and seeded.failed_call_seq
    log = tmp_path / wid / ".state" / "sessions" / sid / "messages.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(json.dumps(r) + "\n" for r in seeded.records), encoding="utf-8")

    open_session_in_studio(page, console_url, wid, sid, kind="agent")
    expect(page.get_by_text(seed.PARENT_FINAL, exact=False).first).to_be_visible(timeout=20_000)

    # (1) the grandchild's fatal failure: a red card inside the CHILD's call block, under the agent's name, with the provider's words.
    parents_rows = page.get_by_test_id(f"nv-subagent-rows:{seeded.parent_call_seq}")
    expect(parents_rows).to_be_visible(timeout=20_000)
    childs_rows = parents_rows.get_by_test_id(f"nv-subagent-rows:{seeded.child_call_seq}")
    expect(childs_rows).to_be_visible()
    failure = childs_rows.get_by_test_id(f"nv-subagent-failure:{seeded.delegated_failure_seq}")
    expect(failure).to_be_visible()
    expect(failure.locator(".nv-turn-error")).to_contain_text(seed.GRANDCHILD_FAILURE)
    expect(failure.locator(".nv-subagent-name")).to_have_text("grand")

    # (2) the second call: its run's notice, drawn as the failure of a call that failed. One red card in ITS block, in the notice's words, no quiet line.
    flaky_rows = page.get_by_test_id(f"nv-subagent-rows:{seeded.failed_call_seq}")
    expect(flaky_rows).to_be_visible()
    card = flaky_rows.get_by_test_id(f"nv-subagent-failure:{seeded.delegated_notice_seq}")
    expect(card).to_be_visible()
    expect(card.locator(".nv-turn-error")).to_contain_text(seed.CHILD_NOTICE)
    expect(card.locator(".nv-subagent-name")).to_have_text("flaky")
    expect(flaky_rows.locator(".nv-turn-note")).to_have_count(0)
    expect(page.get_by_text("the turn is continuing")).to_have_count(0)
    expect(page.get_by_test_id(f"nv-tool:{seeded.failed_call_seq}")).to_contain_text("failed")

    # Containment, both ways: each card is only in its own block (the first call's rows include the grandchild's, so check the failed call's card is not there).
    expect(parents_rows.get_by_test_id(f"nv-subagent-failure:{seeded.delegated_notice_seq}")).to_have_count(0)
    expect(childs_rows.get_by_test_id(f"nv-subagent-failure:{seeded.delegated_notice_seq}")).to_have_count(0)
    expect(flaky_rows.get_by_test_id(f"nv-subagent-failure:{seeded.delegated_failure_seq}")).to_have_count(0)

    # (3) nothing else is a red card: the parent's turn did not fail.
    expect(page.locator(".nv-turn-error")).to_have_count(2)
