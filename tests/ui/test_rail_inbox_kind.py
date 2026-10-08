"""US-007 R2 phase 1 review finding #2, fixed in phase 2 (step 0).

The cross-workspace aggregate GET /yields/pending (primer/api/routers/
workspaces.py::list_pending_attention) collapses to three kinds:
approval/ask/parked. NV_Rail_inboxKindLabel only recognized "approval" and
the OTHER endpoint's "ask_user", so an ask_user yield through the live
aggregate rendered "parked on you" instead of "asking you".
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAIL = (
    ROOT / "ui" / "components" / "console" / "nv-rail.jsx"
).read_text(encoding="utf-8")


def test_label_recognizes_both_ask_spellings() -> None:
    body = RAIL[RAIL.index("function NV_Rail_inboxKindLabel"):]
    body = body[:body.index("\n}")]
    assert 'kind === "ask"' in body
    assert 'kind === "ask_user"' in body
    assert 'return "asking you"' in body


def test_label_still_recognizes_approval_and_defaults_to_parked() -> None:
    body = RAIL[RAIL.index("function NV_Rail_inboxKindLabel"):]
    body = body[:body.index("\n}")]
    assert 'kind === "approval"' in body
    assert 'return "approval"' in body
    assert 'return "parked on you"' in body


def test_the_aggregate_call_goes_through_sh_api() -> None:
    # Nit fix: the raw window.primerApi.apiFetch bypassed SH_api's
    # convention every sibling call in this file uses.
    assert "SH_api.pendingAttention(signal)" in RAIL
    assert 'window.primerApi.apiFetch("GET", "/yields/pending"' not in RAIL


def test_the_404_fallback_still_exists_for_older_servers() -> None:
    assert 'err.status !== 404' in RAIL
    assert "SH_api.pendingYields" in RAIL


def test_sh_api_exposes_the_aggregate_wrapper() -> None:
    api_src = (
        ROOT / "ui" / "components" / "shell" / "sh-api.jsx"
    ).read_text(encoding="utf-8")
    assert "pendingAttention: function (signal)" in api_src
    assert '"/yields/pending"' in api_src
    assert "pendingAttention: function () { return " in api_src


def test_bundle_transpiles_with_the_fix() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(ROOT / "ui")
    assert etag and body


def _line():
    from py_mini_racer import MiniRacer

    start = RAIL.index("function NV_Rail_inboxLine")
    end = RAIL.index("\n}\n", start) + len("\n}\n")
    ctx = MiniRacer()
    ctx.eval(RAIL[start:end])
    return ctx


def test_a_rail_row_says_what_it_is_about() -> None:
    """Console review C-033: an approval row read only "approval" next to a session name."""
    import json

    ctx = _line()
    try:
        def line(item):
            return ctx.eval("NV_Rail_inboxLine(" + json.dumps(item) + ")")

        approval = {"kind": "approval", "approval": {"tool_name": "bash", "arguments": "command=ls -la", "truncated": False}}
        assert line(approval) == "bash command=ls -la"
        assert line({"kind": "approval", "approval": {"tool_name": "bash", "arguments": ""}}) == "bash"
        assert line({"kind": "approval", "approval": None}) == "", "nothing to say, so no line is drawn"
        assert line({"kind": "ask", "prompt": "Which environment?"}) == "Which environment?"
        assert line({"kind": "parked"}) == "" and line({"kind": "parked", "prompt": "30s"}) == "30s"
    finally:
        ctx.close()


def test_the_rail_draws_the_line_only_when_there_is_one() -> None:
    assert 'data-testid={"nv-rail-inbox-line:" + it.session_id}' in RAIL
    assert "NV_Rail_inboxLine(it) ? (" in RAIL
