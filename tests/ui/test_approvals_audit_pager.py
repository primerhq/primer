"""The approvals decisions audit has a filter and a pager of its own (Platform revamp Plan 01, Task 12; design 13.5).

The audit table under the approval policies showed the thirty newest decisions and nothing else: no filter, no way to see the rest. It now has the kit's header (``PK_PageHeader`` inside
``nv-plat-audit``): the title "Decisions audit", its count, the sort select and direction button it always had (as the header's ``extra``), a filter ("Filter decisions", ``nv-audit-filter``) and a
pager, twenty rows a page. The rows are sorted first, then filtered on what a row says (the session, the tool, the status and who decided), then paged, so sorting by oldest puts the oldest decision
on page one.

``NV_ApprovalsAudit`` runs here for real on the page harness (``tests/ui/_platform_page_harness.py``); its three resources (the parked sessions, the decision records and one pending record per parked
session) are answered by name, in place of the harness's one answer for every key.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.ui._platform_page_harness import platform_page_context

# 25 resolved decisions, session s0 the oldest and s24 the newest; every fifth one is rejected, and the tool of decision i is web__fetch<i>.
ANSWERS = """
var ANSWERS = {};
function RECORDS(n) {
  var out = [];
  for (var i = 0; i < n; i++) {
    out.push({ id: "r" + i, session_id: "s" + i, toolset_id: "web", tool_name: "fetch" + i, decision: i % 5 === 4 ? "rejected" : "approved",
               decided_by: "ana", reason: "", requested_at: new Date(Date.UTC(2026, 9, 1, 8, i)).toISOString(), decided_at: new Date(Date.UTC(2026, 9, 1, 9, i)).toISOString() });
  }
  return out;
}
(function () {
  window.primerApi.useResource = function (key, fn, opts) {
    RESOURCE_OPTS[key] = opts || null;
    var data = Object.prototype.hasOwnProperty.call(ANSWERS, key) ? ANSWERS[key] : undefined;
    return { data: data, loading: false, error: null, degraded: false, refetch: function () {} };
  };
})();
"""


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


@pytest.fixture
def audit():
    ctx = platform_page_context(prelude_extra=ANSWERS)
    try:
        yield ctx
    finally:
        ctx.close()


def _mount(ctx, n: int = 25) -> None:
    ctx.eval(f'ANSWERS["nv-plat:approval-records"] = {{ items: RECORDS({n}) }}; ANSWERS["nv-plat:audit-parked-sessions"] = {{ items: [] }}; MR.mount(NV_ApprovalsAudit, {{}});')


def _sessions(ctx) -> list[str]:
    """The session ids of the rows drawn, in order (the first cell of each ``nv-audit-row``)."""
    return [t for t in js(ctx, "MR.texts()") if re.fullmatch(r"s\d+", t)]


def _range(ctx) -> str:
    return ctx.eval("MR.find('pk-pager-range').props.children")


def _filter(ctx, text: str) -> None:
    ctx.eval(f'MR.find("nv-audit-filter").props.onChange({{ target: {{ value: {json.dumps(text)} }} }}); MR.rerender();')


def test_twenty_five_decisions_page_twenty_then_five(audit) -> None:
    _mount(audit)
    assert len(_sessions(audit)) == 20 and _range(audit) == "1-20 of 25"
    assert _sessions(audit)[0] == "s24", "newest first, as before"
    audit.eval("MR.click('pk-pager-next');")
    assert len(_sessions(audit)) == 5 and _range(audit) == "21-25 of 25"
    assert _sessions(audit) == ["s4", "s3", "s2", "s1", "s0"]
    assert audit.eval("MR.find('pk-pager-next').props.disabled") is True
    audit.eval("MR.click('pk-pager-prev');")
    assert len(_sessions(audit)) == 20 and _range(audit) == "1-20 of 25"


def test_the_thirty_row_cut_is_gone(audit) -> None:
    _mount(audit, 45)
    assert len(_sessions(audit)) == 20 and _range(audit) == "1-20 of 45"
    audit.eval("MR.click('pk-pager-next'); MR.click('pk-pager-next');")
    assert _range(audit) == "41-45 of 45" and len(_sessions(audit)) == 5, "the rows past the thirtieth are reachable"


def test_the_header_is_the_kits_with_the_sort_controls_the_filter_and_the_pager(audit) -> None:
    _mount(audit)
    ids = [e.get("testid") for e in js(audit, 'MR.subtree("pk-header")') if e.get("testid")]
    assert ids.index("nv-audit-sort-by") < ids.index("nv-audit-sort-dir") < ids.index("nv-audit-filter") < ids.index("pk-pager")
    texts = js(audit, "MR.texts()")
    assert "Decisions audit" in texts and "25 decisions" in texts
    assert audit.eval('MR.find("nv-audit-filter").props["aria-label"]') == "Filter decisions"
    assert audit.eval('MR.find("nv-audit-sort-by").props["aria-label"]') == "Sort decisions"
    assert audit.eval('MR.find("pk-header").props.children[0].type') == "h2"


def test_the_filter_matches_tool_and_status_and_resets_to_page_one(audit) -> None:
    _mount(audit)
    audit.eval("MR.click('pk-pager-next');")
    assert _range(audit) == "21-25 of 25"
    _filter(audit, "fetch1")                       # fetch1 and fetch10 to fetch19: 11 decisions
    assert _range(audit) == "1-11 of 11", "a filter typed on page two returns to page one"
    assert "11 of 25 decisions" in js(audit, "MR.texts()")
    _filter(audit, "REJECTED")                     # i = 4, 9, 14, 19 and 24, whatever the case
    assert _sessions(audit) == ["s24", "s19", "s14", "s9", "s4"]
    _filter(audit, "s2")                           # the session column: s2 and s20 to s24
    assert sorted(_sessions(audit)) == sorted(["s2", "s20", "s21", "s22", "s23", "s24"])
    _filter(audit, "ana")                          # who decided
    assert _range(audit) == "1-20 of 25"


def test_sorting_happens_before_paging(audit) -> None:
    _mount(audit)
    assert _sessions(audit)[0] == "s24"
    audit.eval("MR.click('nv-audit-sort-dir');")
    assert _sessions(audit)[0] == "s0", "oldest first puts the oldest decision on page one"
    assert _range(audit) == "1-20 of 25" and _sessions(audit)[-1] == "s19"
    audit.eval("MR.click('pk-pager-next');")
    assert _sessions(audit) == ["s20", "s21", "s22", "s23", "s24"]


def test_a_sort_by_status_pages_the_sorted_rows(audit) -> None:
    _mount(audit)
    audit.eval('MR.find("nv-audit-sort-by").props.onChange({ target: { value: "status" } }); MR.rerender();')
    rejected = {"s24", "s19", "s14", "s9", "s4"}
    first = _sessions(audit)
    assert len(first) == 20 and not rejected & set(first), "approved ranks before rejected, so the five rejected rows are all on the last page, not spread over the pages"
    audit.eval("MR.click('pk-pager-next');")
    assert _sessions(audit) == ["s24", "s19", "s14", "s9", "s4"]


def test_the_audit_title_has_no_em_dash(audit) -> None:
    _mount(audit)
    texts = js(audit, "MR.texts()")
    assert "Decisions audit" in texts
    assert not [t for t in texts if chr(0x2014) in t], "the title (and the page) draw no long dash"


def test_the_empty_and_filtered_empty_texts(audit) -> None:
    _mount(audit, 0)
    assert "No decisions recorded yet." in js(audit, "MR.texts()")
    _filter(audit, "zzz")
    assert "No decisions recorded yet." in js(audit, "MR.texts()"), "nothing to match against: the audit is empty, whatever the filter"
    audit.eval('ANSWERS["nv-plat:approval-records"] = { items: RECORDS(3) }; MR.rerender();')
    assert 'No decisions match "zzz".' in js(audit, "MR.texts()")
    assert "No decisions recorded yet." not in js(audit, "MR.texts()")
    assert _range(audit) == "0 of 0" and "0 of 3 decisions" in js(audit, "MR.texts()")
    _filter(audit, "")
    assert len(_sessions(audit)) == 3


def test_a_pending_decision_is_a_row_too(audit) -> None:
    """A parked session's pending record is merged into the rows, and the filter finds it by its status."""
    audit.eval('ANSWERS["nv-plat:approval-records"] = { items: RECORDS(2) }; ANSWERS["nv-plat:audit-parked-sessions"] = { items: [{ id: "sp" }] };'
               'ANSWERS["nv-plat:audit-pending:sp"] = { gate_id: "g1", tool_call_id: "t1", tool_name: "workspaces__write_file", approvers: null, parked_at: "2026-10-01T12:00:00Z" };'
               "MR.mount(NV_ApprovalsAudit, {});")
    assert "3 decisions" in js(audit, "MR.texts()")
    _filter(audit, "pending")
    assert "sp" in js(audit, "MR.texts()") and _range(audit) == "1-1 of 1"


def test_the_polling_of_the_audits_resources_is_unchanged(audit) -> None:
    _mount(audit)
    assert js(audit, 'RESOURCE_OPTS["nv-plat:approval-records"]') == {"pollMs": 5000}
    assert js(audit, 'RESOURCE_OPTS["nv-plat:audit-parked-sessions"]') == {"pollMs": 5000}
