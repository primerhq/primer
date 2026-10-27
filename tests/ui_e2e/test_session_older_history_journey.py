"""The session transcript's older-history seam, end to end (G9-2). Default-skipped (conftest ignores
test_*.py unless PRIMER_RUN_UI_E2E=1).

The history read is the newest 200 records of the session's span, and the endpoint pages older rows
from that tail (``tail=1`` + ``offset``, ``primer/api/routers/sessions.py``). This journey seeds a
session whose transcript is 260 records long, opened straight into the console (the server and this
process share a filesystem in the CI lane, so the log is written into the local workspace's state
directory, where the sessions router reads it), and asserts on the DOM:

* the seam at the top of the transcript names the 60 records the tail window did not fetch;
* one press reads exactly the older page and the oldest seeded record becomes visible, rendered
  BEFORE the newest ones (the older page precedes the tail window in the transcript);
* the span is fully loaded once the older page lands: the seam is gone and the transcript is no
  longer truncated.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e import _session_seed
from tests.ui_e2e._studio_helpers import open_session_in_studio

pytestmark = smk("SMK-UI-G92", status="partial")

# 260 records: the tail window holds the newest 200 (seq 201..260), so 60 records sit behind the seam.
N_RECORDS = 260


def _record_text(seq: int) -> str:
    return f"oldest-history-record-{seq}"


@pytest.fixture
def older_history_session(base_url: str, tmp_path, unique_suffix: str) -> Iterator[_session_seed.SeededSession]:
    """A local-workspace agent session with a 260-record transcript written into its ``messages.jsonl``,
    and its rows taken away again when the journey ends, whatever it did."""
    rows = _session_seed.seed_session(base_url, tmp_path, unique_suffix, description="older history paging probe")
    log = tmp_path / rows.wid / ".state" / "sessions" / rows.sid / "messages.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "seq": seq,
            "kind": "user_input" if seq % 2 else "assistant_token",
            "turn_no": (seq + 1) // 2,
            "payload": {"text": _record_text(seq)},
        }
        for seq in range(1, N_RECORDS + 1)
    ]
    log.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    try:
        yield rows
    finally:
        left = _session_seed.delete_seeded(base_url, rows)
        assert not left, f"seeded rows that could not be deleted: {left}"


def test_the_oldest_records_are_reachable_behind_the_seam(page: Page, console_url: str, older_history_session: _session_seed.SeededSession) -> None:
    wid, sid = older_history_session.wid, older_history_session.sid
    open_session_in_studio(page, console_url, wid, sid)

    # The tail window holds the newest 200 records; the seam names the 60 that sit behind it.
    seam = page.get_by_test_id("sd-load-older")
    expect(seam).to_contain_text("Load older messages (60 more)", timeout=20_000)

    # The oldest record is not on screen yet (that is the defect this seam fixes).
    expect(page.get_by_text(_record_text(1), exact=True)).to_have_count(0)

    # One press reads the older page and the oldest record becomes visible.
    seam.click()
    expect(page.get_by_text(_record_text(1), exact=True)).to_be_visible(timeout=20_000)

    # The older page renders BEFORE the tail window: record 1 above record 250.
    top = page.get_by_text(_record_text(1), exact=True).bounding_box()
    bottom = page.get_by_text(_record_text(250), exact=True).bounding_box()
    assert top and bottom and top["y"] < bottom["y"], "the older page precedes the tail window in the transcript"

    # The span is fully loaded (260 = 200 rendered + 60 loaded): the seam is gone, the transcript is
    # no longer truncated, and nothing else is read (the seam had nothing left to offer).
    expect(page.get_by_test_id("sd-load-older")).to_have_count(0)
    expect(page.get_by_text(_record_text(N_RECORDS), exact=True)).to_be_visible()
