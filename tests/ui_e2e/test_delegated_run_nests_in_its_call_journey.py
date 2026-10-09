"""UI e2e: a delegated run renders INSIDE the tool call that delegated to it. Default-skipped (conftest ignores test_*.py unless
PRIMER_RUN_UI_E2E=1).

The console's subagent nesting never worked on real records: it keyed on ``payload.tool_call_id``, which the persistence layer does not
write (it writes ``id`` and ``raw_id``), and the call's block did not draw nested rows anyway. This journey seeds a session log with a
REAL delegated run (``tests/ui_e2e/_delegation_seed.py``: the records come from ``translate_stream_event`` and the real
``DelegationRecorder``) and asserts, on the DOM, where each row ends up:

* the child run's rows render inside the parent's ``invoke_agent`` call block (containment, not just presence);
* the grandchild run's rows render inside the CHILD's own call block, although that call reuses the raw id ``call_0`` of its parent;
* no delegated row renders at the top level of the transcript.

The server and this process share a filesystem in the CI lane (a host ``primer api``), so the log is written straight into the local
workspace's state directory, where the sessions router and the tap read it from.
"""

from __future__ import annotations

import json

from playwright.sync_api import expect

from tests.ui_e2e import _delegation_seed as seed
from tests.ui_e2e._session_seed import seed_session
from tests.ui_e2e._studio_helpers import open_session_in_studio


def test_a_delegated_run_renders_inside_the_call_that_delegated_to_it(base_url, console_url, page, tmp_path, unique_suffix) -> None:
    rows = seed_session(base_url, tmp_path, unique_suffix, description="delegation nesting probe")
    wid, sid = rows.wid, rows.sid
    seeded = seed.build()
    log = tmp_path / wid / ".state" / "sessions" / sid / "messages.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(json.dumps(r) + "\n" for r in seeded.records), encoding="utf-8")

    open_session_in_studio(page, console_url, wid, sid, kind="agent")

    # The parent's own turn is on screen at the top level.
    expect(page.get_by_text(seed.PARENT_FINAL, exact=False).first).to_be_visible(timeout=20_000)

    # (1) the child run's rows are INSIDE the invoke_agent call's block: the wrapper that holds the call's block holds its rows.
    parents_block = page.locator(".nv-call-with-subagents").filter(
        has=page.get_by_test_id(f"nv-tool:{seeded.parent_call_seq}"),
    ).first
    parents_rows = parents_block.get_by_test_id(f"nv-subagent-rows:{seeded.parent_call_seq}")
    expect(parents_rows).to_be_visible(timeout=20_000)
    expect(parents_rows.get_by_text(seed.CHILD_BEFORE, exact=False)).to_be_visible()
    expect(parents_rows.get_by_text(seed.CHILD_AFTER, exact=False)).to_be_visible()

    # (2) the grandchild run's rows are inside the CHILD's call block, which is itself inside the parent's rows, although both calls
    # carry the raw id call_0.
    childs_rows = parents_rows.get_by_test_id(f"nv-subagent-rows:{seeded.child_call_seq}")
    expect(childs_rows).to_be_visible()
    expect(childs_rows.get_by_text(seed.GRANDCHILD, exact=False)).to_be_visible()
    childs_block = parents_rows.locator(".nv-call-with-subagents").filter(
        has=page.get_by_test_id(f"nv-tool:{seeded.child_call_seq}"),
    ).first
    expect(childs_block.get_by_test_id(f"nv-subagent-rows:{seeded.child_call_seq}")).to_be_visible()

    # (3) nothing delegated renders at the top level: each delegated text appears exactly once (nested, not also top level), and no
    # delegated assistant row has a top-level turn element.
    for text in (seed.CHILD_BEFORE, seed.GRANDCHILD, seed.CHILD_AFTER):
        expect(page.get_by_text(text, exact=False)).to_have_count(1)
    for seq in seeded.delegated_assistant_seqs:
        expect(page.locator(f'[data-testid="nv-turn:{seq}"]')).to_have_count(0)
    # ... while the parent's own answer is a top-level turn.
    for seq in seeded.top_level_assistant_seqs:
        expect(page.locator(f'[data-testid="nv-turn:{seq}"]')).to_have_count(1)
