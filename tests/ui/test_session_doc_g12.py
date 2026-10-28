"""Red probes for the G12 session-doc batch (G12-05, G12-01 UI, G12-07,
G12-09 composer chip).

* G12-05 the session doc's voice auto-play effect is fenced behind
  ``typeof window.CT_speakTurn === "function"`` and no bundle file ever
  assigned it (the real player is the module-private ``SC_speakTurn`` in
  shared/session-controls.jsx) - the seam is dead;
* G12-01 a failed turn's error node renders as a red-toned one-liner in
  the trace split (the backend half is
  tests/session/test_timeline_error_node.py);
* G12-07 the trace split confuses a failed or still-loading timeline
  fetch with an empty turn (no ``timeline.error``/``timeline.loading``
  branch);
* G12-09 a vision attachment the server drops leaves the chip "done": the
  committed chip has no dropped state.

MiniRacer over the committed source (the register-overlay harness shape);
scalar eval returns only.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx"
SC = ROOT / "ui" / "components" / "shared" / "session-controls.jsx"
SRC = DOC.read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = SRC.index("function " + name + "(")
    return SRC[start : SRC.index("\n}\n", start) + len("\n}\n")]


def _transform(code: str, name: str) -> str:
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    bundler = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    try:
        return bundler._transform(code, name)
    finally:
        bundler._ctx.close()


# ---- G12-05: the voice seam is live -------------------------------------------


def test_the_doc_voice_seam_is_a_live_function() -> None:
    code = transpile(SC)
    ctx = mini_react_context(code)
    try:
        assert ctx.eval("typeof window.CT_speakTurn === 'function'") is True, (
            "the doc's auto-play guard (typeof window.CT_speakTurn === "
            '"function") can never pass: no bundle file assigns '
            "window.CT_speakTurn"
        )
    finally:
        ctx.close()

    sc_src = SC.read_text(encoding="utf-8")
    assert "window.CT_speakTurn = SC_speakTurn" in sc_src, (
        "the alias must BE the real player (SC_speakTurn), not a second "
        "implementation"
    )
    # The doc's guard + call ride the same seam, and the off-toggle pauses
    # the element the seam returns (the stop path).
    assert 'typeof window.CT_speakTurn === "function"' in SRC
    assert "window.CT_speakTurn(" in SRC
    assert "con.voiceRef.current.pause()" in SRC


# ---- G12-01 UI + G12-07: the trace split --------------------------------------

# The committed trace split plus the helpers its rows call (the extraction
# is the house _function pattern, transpiled once for the module).
_SPLIT_NAMES = (
    "NV_traceAgentName",
    "NV_traceRowLabel",
    "NV_traceCompact",
    "NV_traceTokens",
    "NV_traceElapsed",
    "NV_traceGlyph",
    "NV_TraceGlyph",
    "NV_TraceLine",
    "NV_TraceSplit",
)


@functools.lru_cache(maxsize=1)
def _split_code() -> tuple[str, str]:
    snippet = _transform("\n".join(_function(n) for n in _SPLIT_NAMES), "snippet.jsx")
    prelude = (
        (ROOT / "ui" / "foundation" / "shell-turns.js").read_text(encoding="utf-8")
        + "\nvar SNAPSHOT = null;\nvar REFETCHED = false;\n"
        + "var SH_api = { keys: { timeline: function (sid, n) { return 'tl:' + sid + ':' + n; } }, "
        + "timeline: function (sid, n, signal) { return Promise.resolve(null); } };\n"
        + "window.primerApi = window.primerApi || {};\n"
        + "window.primerApi.useResource = function (key, fetcher, opts) { return SNAPSHOT; };\n"
    )
    return snippet, prelude


def _split_ctx(snapshot_js: str):
    snippet, prelude = _split_code()
    ctx = mini_react_context(snippet, prelude)
    ctx.eval(
        "REFETCHED = false; SNAPSHOT = " + snapshot_js + ";\n"
        "MR.mount(NV_TraceSplit, { sid: 's1', turnNo: 0, agentName: null, "
        "isGraph: false, onClose: function () {} });"
    )
    return ctx


def _texts(ctx) -> str:
    return " ".join(json.loads(ctx.eval("JSON.stringify(MR.texts())")))


def test_the_trace_split_renders_the_error_node_in_the_error_tone() -> None:
    error_node = {
        "kind": "error",
        "seq": 2,
        "ts": "2026-10-09T12:00:02+00:00",
        "code": "llm_unavailable",
        "message": "boom",
        "children": [],
    }
    snapshot = (
        "{ data: { children: " + json.dumps([error_node]) + " }, error: null, "
        "loading: false, degraded: false, refetch: function () { REFETCHED = true; } }"
    )
    ctx = _split_ctx(snapshot)
    try:
        assert ctx.eval("MR.findProp('data-tone', 'error') != null"), (
            "the error node renders with no error tone in the split"
        )
        texts = _texts(ctx)
        assert "boom" in texts and "llm_unavailable" in texts, texts
    finally:
        ctx.close()


def test_the_trace_split_distinguishes_a_failed_fetch_from_empty() -> None:
    snapshot = (
        "{ data: null, error: { detail: 'session not found', status: 404 }, "
        "loading: false, degraded: false, "
        "refetch: function () { REFETCHED = true; } }"
    )
    ctx = _split_ctx(snapshot)
    try:
        texts = _texts(ctx)
        assert "Couldn't load the trace" in texts, (
            "a failed timeline fetch (404/503) renders like an empty turn"
        )
        assert "session not found" in texts, texts
        ctx.eval("MR.click('nv-trace-retry')")
        assert ctx.eval("REFETCHED") is True, "the Retry does not refetch"
    finally:
        ctx.close()


def test_the_trace_split_shows_a_loading_line_before_first_data() -> None:
    snapshot = (
        "{ data: null, error: null, loading: true, degraded: false, "
        "refetch: function () { REFETCHED = true; } }"
    )
    ctx = _split_ctx(snapshot)
    try:
        texts = _texts(ctx)
        assert "Loading trace" in texts, "no loading line before first data"
        assert "Couldn't load the trace" not in texts, texts
    finally:
        ctx.close()


# ---- G12-09: the chip carries a dropped state ----------------------------------


def test_the_chip_carries_a_dropped_state() -> None:
    chips = SRC[SRC.index('data-testid="nv-attach-chips"') :]
    chips = chips[: chips.index("nv-composer-row")]
    m = re.search(r'a\.status === "dropped" \? \(\s*(<span[\s\S]*?)\) : null', chips)
    assert m, "the committed chip has no dropped state (G12-09)"
    assert "nv-attach-chip-dropped" in m.group(1)
    assert "not sent to the model" in m.group(1)

    # The committed branch, transpiled and rendered with a fake attachment.
    snippet = _transform(
        "function chipDrop(a) { return " + m.group(1) + "; }", "chip.jsx"
    )
    ctx = mini_react_context(snippet)
    try:
        ctx.eval("var el = chipDrop({ id: 'x', reason: 'unreadable' });")
        props = json.loads(ctx.eval("JSON.stringify(el.props)"))
        assert props["data-testid"] == "nv-attach-chip-dropped:x", props
        assert "dropped (not sent to the model): unreadable" in props["title"], props
    finally:
        ctx.close()


# ---- G12-07 fix: the hooks precede the early branches -------------------------------------------


def test_the_trace_split_hooks_run_before_the_early_branches() -> None:
    """React's rules of hooks: a useState after an early return changes the
    hook count between renders, and error #310 tears the component down the
    moment data lands after a loading or error branch (the CI red on the
    healthy-turn trace journeys). Every hook call in NV_TraceSplit must sit
    ahead of its first early return."""
    body = _function("NV_TraceSplit")
    hooks = [m.start() for m in re.finditer(r"React\.use[A-Za-z]+|primerApi\.use[A-Za-z]+", body)]
    assert hooks, "NV_TraceSplit lost its hooks"
    early = body.index("return (")
    assert max(hooks) < early, "a hook of NV_TraceSplit runs after the first early return (React error #310)"
