"""T132 (GU-8, GU-10, GU-14, GU-18): inbox totals, the phone Files error states, the admin token fan-out, the tool picker's unavailable toolsets."""

from __future__ import annotations

import json
import re
from pathlib import Path

from py_mini_racer import MiniRacer

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"


def _read(*parts: str) -> str:
    return (UI.joinpath(*parts)).read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    """The text of top-level ``function name(...) {...}`` (it ends at the first line that is just a closing brace)."""
    start = src.index(f"function {name}(")
    return src[start : src.index("\n}\n", start) + 3]


def test_the_attention_total_is_the_servers_count_never_below_the_rows_in_hand() -> None:
    ctx = MiniRacer()
    ctx.eval(_fn(_read("components", "shell", "sh-api.jsx"), "SH_attentionTotal"))
    assert ctx.eval("SH_attentionTotal({items: new Array(100).fill(0), total: 250})") == 250
    assert ctx.eval("SH_attentionTotal({items: [1, 2], total: 2})") == 2
    assert ctx.eval("SH_attentionTotal({items: [1, 2, 3]})") == 3
    assert ctx.eval("SH_attentionTotal({items: [1, 2, 3], total: 1})") == 3
    assert ctx.eval("SH_attentionTotal(null)") == 0


def test_the_phone_heading_says_how_many_wait_not_how_many_are_shown() -> None:
    ctx = MiniRacer()
    ctx.eval(_fn(_read("components", "console", "nv-mobile-shell.jsx"), "NV_mobileInboxHeading"))
    assert json.loads(ctx.eval("JSON.stringify(NV_mobileInboxHeading(100, true, 250))"))["count"] == "250 waiting on you"
    assert json.loads(ctx.eval("JSON.stringify(NV_mobileInboxHeading(3, true))"))["count"] == "3 waiting on you"
    assert json.loads(ctx.eval("JSON.stringify(NV_mobileInboxHeading(0, true))"))["count"] == "Nothing waiting on you"


def test_the_tab_label_the_rail_count_and_the_note_use_the_total() -> None:
    mobile = _read("components", "console", "nv-mobile-shell.jsx")
    assert "inboxTotal" in mobile and 'data-testid="nv-mob-ib-cut"' in mobile
    assert '"Inbox" + (inboxItems.length' not in mobile
    assert "SH_attentionTotal(inboxRes.data)" in _read("components", "console", "nv-rail.jsx")


def test_the_decisions_audit_reads_every_page_of_records() -> None:
    api = _read("components", "shell", "sh-api.jsx")
    block = api[api.index("approvalRecords:"):]
    block = block[: block.index("\n  },")]
    assert "PR_fetchAllPages" in block and "length=200" not in block
    ctx = MiniRacer()
    ctx.eval(_fn(_read("foundation", "api.js"), "PR_pageUrl"))
    assert ctx.eval('PR_pageUrl("/tool_approval/records?status=all", 200, 200, "length")') == "/tool_approval/records?length=200&offset=200&status=all"
    assert ctx.eval('PR_pageUrl("/x", 200, 0)') == "/x?limit=200&offset=0"


def test_a_failed_phone_files_read_says_so_with_retry_instead_of_reading_empty() -> None:
    src = _read("components", "console", "nv-mobile-shell.jsx")
    assert "function NV_MobileReadError(" in src
    for key in ("tree", "commits", "read"):
        assert re.search(rf"{key}\.error && !{key}\.data", src), key
    body = src[src.index("function NV_MobileReadError("):]
    body = body[: body.index("\n}\n")]
    assert "NV_errText(props.error)" in body and "onRetry" in body and 'role="alert"' in body


def test_the_admin_token_page_keeps_at_most_four_user_reads_in_flight() -> None:
    ctx = MiniRacer()
    ctx.eval("var window = globalThis; window.React = {};")
    ctx.eval(_read("foundation", "api.js"))
    ctx.eval(_fn(_read("components", "api_tokens.jsx"), "AT_readAdminTokens").replace("function AT_readAdminTokens", "function AT_readAdminTokens"))
    ctx.eval(
        "var live = 0, peak = 0, out = null;"
        "var users = []; for (var i = 0; i < 12; i++) users.push({id: 'u' + i, username: 'n' + i});"
        "function api(method, path) {"
        " if (path === '/admin/users') return Promise.resolve({items: users});"
        " live += 1; peak = Math.max(peak, live);"
        " return new Promise(function (r) { Promise.resolve().then(function () { live -= 1; r({items: [{id: 't' + path}]}); }); }); }"
        "AT_readAdminTokens(api, undefined).then(function (v) { out = v; });"
    )
    for _ in range(40):
        ctx.eval("void 0;")
    assert ctx.eval("peak") <= 4
    assert ctx.eval("out.items.length") == 12 and ctx.eval("out.items[0]._ownerId") == "u0"


def test_selected_tools_of_an_unavailable_toolset_are_labelled_unavailable_not_stale() -> None:
    ctx = MiniRacer()
    ctx.eval(_fn(_read("components", "shared", "tool-picker.jsx"), "TP_splitSelected"))
    split = json.loads(ctx.eval(
        "JSON.stringify(TP_splitSelected(new Set(['mcp_x__a', 'web__fetch', 'gone__tool', 'web__old']),"
        " [{id: 'mcp_x', available: false, tools: []}, {id: 'web', available: true, tools: [{scoped_id: 'web__fetch'}]}]))"
    ))
    assert split == {"stale": ["gone__tool", "web__old"], "unavailable": ["mcp_x__a"]}
    src = _read("components", "shared", "tool-picker.jsx")
    assert 'data-testid="tool-picker-unavailable-selected"' in src
