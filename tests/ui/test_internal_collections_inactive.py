"""The Internal Collections page says plainly that it is off, and asks without a red console error (finding L2 of the 2026-10-08 review).

``InternalCollectionsPage`` probes ``GET /v1/internal_collections/config`` on every open. When the subsystem is not configured the route
answered 404, which the page already treated as "off", but the browser logs every 404 fetch as a red ``Failed to load resource`` console
error, so opening the page on a fresh install put an error in the console each time. The page now asks with ``?allow_missing=true`` and the
route answers ``200 {"configured": false}`` (the default stays 404, for every other client). The not-configured card also spoke API:
"The four /v1/{kind}/search routes return 503 until this subsystem is active."

The probe is a pure async function in ``internal-collections.jsx`` (``_icFetchConfig``), driven here through MiniRacer against the real
source with a fake ``apiFetch``. The card copy is checked as text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "internal-collections.jsx").read_text(encoding="utf-8")

# Every V8 isolate this file creates, closed after each test (tests/ui peaks at over a gigabyte for the isolates nobody disposes).
_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _probe_src() -> str:
    start = SRC.index("async function _icFetchConfig(")
    end = SRC.index("// Poll the bootstrap-status row.")
    return SRC[start:end]


def _probe(answer: str):
    """Run ``_icFetchConfig`` with a fake ``apiFetch`` whose answer is the JS expression ``answer`` (a value, or a rejection). Returns
    ``(requests, outcome)``: every (method, path) asked, and ``{value}`` or ``{error}`` of the promise."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval(
        "var requests = [];"
        "var window = { primerApi: { apiFetch: function (method, path) { requests.push([method, path]); return " + answer + "; } } };"
    )
    ctx.eval(_probe_src())
    ctx.eval(
        "var outcome = null;"
        "_icFetchConfig(null).then(function (v) { outcome = { value: v === undefined ? 'undefined' : v }; },"
        " function (e) { outcome = { error: e && e.status ? e.status : String(e) }; });"
    )
    return json.loads(ctx.eval("JSON.stringify(requests)")), json.loads(ctx.eval("JSON.stringify(outcome)"))


def test_the_probe_asks_with_allow_missing_so_an_unconfigured_subsystem_is_not_a_404() -> None:
    requests, _ = _probe("Promise.resolve({ configured: false })")

    assert requests == [["GET", "/internal_collections/config?allow_missing=true"]]


def test_a_not_configured_answer_is_the_off_state() -> None:
    _, outcome = _probe("Promise.resolve({ configured: false })")

    assert outcome == {"value": None}


def test_a_configured_row_is_passed_through_unchanged() -> None:
    row = '{ embedding_provider_id: "hf-1", activated_at: null }'

    _, outcome = _probe(f"Promise.resolve({row})")

    assert outcome == {"value": {"embedding_provider_id": "hf-1", "activated_at": None}}


def test_a_404_from_a_server_that_ignores_the_flag_is_still_the_off_state() -> None:
    _, outcome = _probe("Promise.reject({ status: 404 })")

    assert outcome == {"value": None}


def test_any_other_failure_is_still_an_error() -> None:
    _, outcome = _probe("Promise.reject({ status: 500 })")

    assert outcome == {"error": 500}


def _inactive_card_src() -> str:
    start = SRC.index("function InactiveCard(")
    return SRC[start : SRC.index("\nfunction ", start + 1)]


def test_the_not_configured_card_speaks_to_a_user_not_to_an_api_client() -> None:
    card = _inactive_card_src()

    assert "/v1/" not in card and "503" not in card and "routes" not in card, "API jargon on the card"
    assert "Semantic search over your agents, graphs, collections and tools is off until you configure it." in card


def test_the_configured_toast_does_not_talk_about_search_routes() -> None:
    card = _inactive_card_src()

    assert "search routes" not in card
