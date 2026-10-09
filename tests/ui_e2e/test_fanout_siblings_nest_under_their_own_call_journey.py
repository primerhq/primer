"""UI e2e: the runs of two fan-out siblings that delegate under the same raw call id render inside their OWN node's call (ticket 01a11cca).

Nodes ``A`` and ``B`` of a graph run at once and each calls ``invoke_agent``; their provider synthesises ``call_0`` for both. Both calls are written before either helper's records, so
the console, which nested a delegated record under the LAST call with its raw id, drew both answers inside node B's call and none inside node A's. The seed
(``tests/ui_e2e/_delegation_seed.py: build_fanout``) is the real recorder's output with ``delegate_node_id`` on the delegated records; the journey asserts the containment on the DOM.
"""

from __future__ import annotations

import json

from playwright.sync_api import expect

from tests.ui_e2e import _delegation_seed as seed
from tests.ui_e2e._studio_helpers import open_session_in_studio
from tests.ui_e2e.test_delegated_run_nests_in_its_call_journey import _seed_session


def test_each_siblings_helper_renders_inside_its_own_nodes_call(base_url, console_url, page, tmp_path, unique_suffix) -> None:
    wid, sid = _seed_session(base_url, tmp_path, unique_suffix)
    seeded = seed.build_fanout()
    log = tmp_path / wid / ".state" / "sessions" / sid / "messages.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(json.dumps(r) + "\n" for r in seeded.records), encoding="utf-8")

    open_session_in_studio(page, console_url, wid, sid, kind="agent")
    expect(page.get_by_text(seed.PARENT_FINAL, exact=False).first).to_be_visible(timeout=20_000)

    rows_a = page.get_by_test_id(f"nv-subagent-rows:{seeded.call_a_seq}")
    rows_b = page.get_by_test_id(f"nv-subagent-rows:{seeded.call_b_seq}")
    expect(rows_a).to_be_visible(timeout=20_000)
    expect(rows_b).to_be_visible()
    expect(rows_a.get_by_text(seed.ANSWER_A, exact=False)).to_be_visible()
    expect(rows_b.get_by_text(seed.ANSWER_B, exact=False)).to_be_visible()
    # and neither answer is inside the other node's call, or drawn twice
    expect(rows_a.get_by_text(seed.ANSWER_B, exact=False)).to_have_count(0)
    expect(rows_b.get_by_text(seed.ANSWER_A, exact=False)).to_have_count(0)
    for text in (seed.ANSWER_A, seed.ANSWER_B):
        expect(page.get_by_text(text, exact=False)).to_have_count(1)
