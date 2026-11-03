"""A turn that could not even start says why in the console (the owner: "we should show the actual error here").

The failure card draws the session's ERROR record (``NV_errorView``): its message is the exception's own words (credentials redacted by the server), and only a record with no
message and no code falls back to "The turn failed.". The footer of an ended session (``NV_endedLine``) reads the code the row stamped (``last_turn_error``): a build failure's
code, ``executor_build_failed``, names the cause in words instead of "the failure code is executor_build_failed". Run in V8 against the real functions.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")
MESSAGE = "The turn could not start: NotFoundError: On-disk session slot for 'sess-prime-main-2' missing on workspace 'prime'"


@functools.lru_cache(maxsize=1)
def _compiled() -> str:
    from primer.api._jsx_bundle import JSXBundler

    parts = []
    for name in ("NV_failureWords", "NV_failureCodeWords", "NV_endedLine", "NV_errorView"):
        start = DOC.index("function " + name + "(")
        parts.append(DOC[start:DOC.index("\n}\n", start) + len("\n}\n")])
    bundler = JSXBundler(ui_dir=ROOT / "ui", babel_source=(ROOT / "ui" / "vendor" / "babel.min.js").read_text())
    try:
        return bundler._transform("\n".join(parts), "snippet.jsx")
    finally:
        bundler._ctx.close()


@pytest.fixture
def v8():
    ctx = mini_react_context(_compiled())
    yield lambda expr: json.loads(ctx.eval(f"JSON.stringify({expr})"))
    ctx.close()


def test_the_failure_card_shows_the_exceptions_own_words(v8):
    view = v8(f"NV_errorView({{payload: {{message: {json.dumps(MESSAGE)}, code: 'executor_build_failed'}}}})")
    assert view["text"] == MESSAGE and "The turn failed" not in view["text"]


def test_a_record_with_nothing_in_it_is_still_the_bare_fallback(v8):
    assert v8("NV_errorView({payload: {}})")["text"] == "The turn failed."


def test_the_ended_footer_names_a_build_failure_in_words(v8):
    line = v8("NV_endedLine({status: 'ended', ended_reason: 'failed', ended_detail: null, last_turn_error: {code: 'executor_build_failed'}})")
    assert "set up" in line["why"] and "transcript" in line["why"] and "failure code is" not in line["why"]
