"""UI e2e: a subagent's failure and its recoverable problem are drawn INSIDE the subagent's own block (console review 2026-10-08, ticket 01a11c1e).

``NV_subagentRows`` drew only the rows that carry a ``label``, and an error row carries none (its words are in ``payload.message``), so a subagent that
FAILED showed nothing in its block: the docs said a subagent's failure is drawn inside its own block, and it was not. This seeds a session with a REAL
delegated run (``tests/ui_e2e/_delegation_seed.py``, ``build(failures=True)``: the records come from ``translate_stream_event`` and the real
``DelegationRecorder``): the grandchild run fails for good, and the child run's stream reports a recoverable problem and carries on.

Asserted on the DOM, by containment:

* the grandchild's failure is a red card INSIDE the child's own call block (where its text is), with the provider's words;
* the child's recoverable problem is the quiet line (not a red card) inside the parent's ``invoke_agent`` block, and it says the run carried on;
* the only red card in the whole transcript is that one: the parent's turn did not fail, and the notice is not a failure.
"""

from __future__ import annotations

import json

from playwright.sync_api import expect

from tests.ui_e2e import _delegation_seed as seed
from tests.ui_e2e._studio_helpers import open_session_in_studio
from tests.ui_e2e.test_delegated_run_nests_in_its_call_journey import _seed_session


def test_a_subagents_failure_and_notice_render_inside_its_block(base_url, console_url, page, tmp_path, unique_suffix) -> None:
    wid, sid = _seed_session(base_url, tmp_path, unique_suffix)
    seeded = seed.build(failures=True)
    assert seeded.delegated_failure_seq and seeded.delegated_notice_seq
    log = tmp_path / wid / ".state" / "sessions" / sid / "messages.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(json.dumps(r) + "\n" for r in seeded.records), encoding="utf-8")

    open_session_in_studio(page, console_url, wid, sid, kind="agent")
    expect(page.get_by_text(seed.PARENT_FINAL, exact=False).first).to_be_visible(timeout=20_000)

    parents_rows = page.get_by_test_id(f"nv-subagent-rows:{seeded.parent_call_seq}")
    expect(parents_rows).to_be_visible(timeout=20_000)
    childs_rows = parents_rows.get_by_test_id(f"nv-subagent-rows:{seeded.child_call_seq}")
    expect(childs_rows).to_be_visible()

    # (1) the grandchild's failure: a red card inside the CHILD's call block, with the provider's words.
    failure = childs_rows.get_by_test_id(f"nv-subagent-failure:{seeded.delegated_failure_seq}")
    expect(failure).to_be_visible()
    expect(failure.locator(".nv-turn-error")).to_contain_text(seed.GRANDCHILD_FAILURE)

    # (2) the child's recoverable problem: the quiet line, in the parent's block, and it says the run carried on.
    notice = parents_rows.get_by_test_id(f"nv-subagent-notice:{seeded.delegated_notice_seq}")
    expect(notice).to_be_visible()
    expect(notice.locator(".nv-turn-note")).to_contain_text("carried on")
    expect(notice.locator(".nv-turn-note")).to_contain_text(seed.CHILD_NOTICE)
    expect(notice.locator(".nv-turn-error")).to_have_count(0)

    # (3) nothing else is a red card: the parent's turn did not fail, and the notice is not a failure.
    expect(page.locator(".nv-turn-error")).to_have_count(1)
