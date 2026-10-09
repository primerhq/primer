"""The System dashboard's health cards say what the server now knows (console review C-036, the console part).

* **sessions active** counted ``GET /sessions?status=running``, which includes a session parked on a yielding tool and one queued for a worker that has not claimed it, so the card said
  "running now" for sessions that were not running. ``GET /sessions?session_state=running`` (the derived state every row serves) is the turn in flight right now.
* **scheduler** said "healthy" and nothing else. The health route serves ``scheduler.detail`` for a healthy scheduler ("in-memory scheduler (single process assumed)" on the default install:
  the process cannot tell that it is the only one), and the card shows it under the word.

``NV_HealthCards`` runs in V8 on the hook runtime of ``tests/ui/_mini_react.py`` with the resource hook answered from a table.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
SYSTEM = (ROOT / "ui" / "components" / "console" / "nv-system.jsx").read_text(encoding="utf-8")

_PRELUDE = r"""
var __data = {}; var __fetchers = {}; var __asked = [];
window.primerApi = {
  useResource: function (key, fetcher) { __fetchers[key] = fetcher; return { data: __data[key] === undefined ? null : __data[key], key: key }; },
  resourceState: function (r) { return r.data ? "ready" : "loading"; },
  apiFetch: function (method, path) { __asked.push([method, path]); return Promise.resolve({}); },
};
var SH_api = { pendingAttention: function () { __asked.push(["GET", "/yields/pending"]); return Promise.resolve({ total: 0 }); } };
// the colour of each card's dot, in card order, as the last render drew them
var __dots = [];
(function () {
  var make = React.createElement;
  React.createElement = function (type, props) {
    if (props && props.className === "nv-health-dot") __dots.push(props.style && props.style.background);
    return make.apply(null, arguments);
  };
})();
"""

_HEALTH_OK = {"scheduler": {"alive": True, "degraded": False, "degraded_reason": None, "detail": "in-memory scheduler (single process assumed)"}, "worker_pool": {"in_flight": 0, "capacity": 4}}


@functools.lru_cache(maxsize=1)
def _compiled() -> str:
    from primer.api._jsx_bundle import JSXBundler

    start = SYSTEM.index("function NV_HealthCards(")
    snippet = SYSTEM[start:SYSTEM.index("\n}\n", start) + 3]
    bundler = JSXBundler(ui_dir=ROOT / "ui", babel_source=(ROOT / "ui" / "vendor" / "babel.min.js").read_text())
    try:
        return bundler._transform(snippet, "snippet.jsx")
    finally:
        bundler._ctx.close()


@pytest.fixture
def cards():
    made: list = []

    def go(health: dict | None, sessions: dict | None = None) -> dict[str, tuple[str, str]]:
        ctx = mini_react_context(_compiled(), _PRELUDE)
        made.append(ctx)
        ctx.eval(f"__data['nv-sys:health'] = {json.dumps(health)}; __data['nv-sys:sessions-active'] = {json.dumps(sessions)};")
        ctx.eval("MR.mount(NV_HealthCards, {});")
        texts = json.loads(ctx.eval("JSON.stringify(MR.texts())"))
        assert len(texts) == 12, texts   # four cards of: name, value, sub-line
        return {texts[i]: (texts[i + 1], texts[i + 2]) for i in range(0, 12, 3)}

    def tones() -> dict[str, str]:
        """The dot colour of each card, from the last render of the last context."""
        dots = json.loads(made[-1].eval("JSON.stringify(__dots.slice(-4))"))
        return dict(zip(("scheduler", "worker pool", "sessions active", "attention"), dots, strict=True))

    go.contexts = made   # type: ignore[attr-defined]
    go.tones = tones   # type: ignore[attr-defined]
    try:
        yield go
    finally:
        for c in made:
            c.close()


def _asked(cards_fixture) -> list[list[str]]:
    return json.loads(cards_fixture.contexts[-1].eval("JSON.stringify((function () { Object.keys(__fetchers).forEach(function (k) { __fetchers[k](); }); return __asked; })())"))


def test_sessions_active_asks_for_the_turns_in_flight_not_every_session_marked_running(cards) -> None:
    cards(_HEALTH_OK, {"total": 2})
    paths = [p for _m, p in _asked(cards)]
    assert "/sessions?session_state=running&limit=1" in paths, paths
    assert not [p for p in paths if "status=running" in p], paths


def test_sessions_active_shows_the_total_it_was_given(cards) -> None:
    assert cards(_HEALTH_OK, {"total": 2})["sessions active"] == ("2", "running now")


def test_sessions_active_is_an_ellipsis_until_the_list_answers(cards) -> None:
    assert cards(_HEALTH_OK, None)["sessions active"][0] == "…"


def test_a_healthy_scheduler_shows_what_the_server_says_it_is(cards) -> None:
    assert cards(_HEALTH_OK, {"total": 0})["scheduler"] == ("alive", "in-memory scheduler (single process assumed)")


def test_a_healthy_scheduler_with_no_detail_still_says_healthy(cards) -> None:
    health = {**_HEALTH_OK, "scheduler": {"alive": True, "degraded": False, "detail": None}}
    assert cards(health, {"total": 0})["scheduler"] == ("alive", "healthy")


def test_a_degraded_scheduler_gives_its_reason_and_never_the_healthy_detail(cards) -> None:
    health = {**_HEALTH_OK, "scheduler": {"alive": True, "degraded": True, "degraded_reason": "in-memory scheduler in a worker-only process", "detail": "in-memory scheduler (single process assumed)"}}
    assert cards(health, {"total": 0})["scheduler"] == ("degraded", "in-memory scheduler in a worker-only process")


def test_no_scheduler_is_down(cards) -> None:
    health = {**_HEALTH_OK, "scheduler": {"alive": False, "degraded": False, "detail": None}}
    assert cards(health, {"total": 0})["scheduler"] == ("down", "no scheduler attached")


def test_an_idle_install_shows_zero_and_not_an_ellipsis(cards) -> None:
    """A total of 0 is an answer. A reading that treated it as missing would show the loading ellipsis on every idle install."""
    assert cards(_HEALTH_OK, {"total": 0, "items": []})["sessions active"] == ("0", "running now")


def test_the_total_wins_over_the_length_of_the_page(cards) -> None:
    """The request asks for one row (``limit=1``): the card is the TOTAL, not the page."""
    assert cards(_HEALTH_OK, {"total": 2, "items": [{}]})["sessions active"] == ("2", "running now")


def test_while_the_health_check_loads_the_scheduler_card_says_it_is_checking_not_that_there_is_none(cards) -> None:
    reading = cards(None, {"total": 0})["scheduler"]
    assert reading == ("\u2026", "checking\u2026"), reading


def test_while_the_health_check_loads_the_scheduler_dot_is_neutral(cards) -> None:
    """Loading is not an alarm: no red, amber or green until the server has answered."""
    cards(None, {"total": 0})
    assert cards.tones()["scheduler"] not in ("var(--red)", "var(--amber)", "var(--green)"), cards.tones()


def test_the_dots_still_mean_what_they_meant_once_the_server_has_answered(cards) -> None:
    """The controls: healthy is green, degraded is amber, not alive is red."""
    cards(_HEALTH_OK, {"total": 0})
    assert cards.tones()["scheduler"] == "var(--green)"
    degraded = {**_HEALTH_OK, "scheduler": {"alive": True, "degraded": True, "degraded_reason": "worker-only"}}
    cards(degraded, {"total": 0})
    assert cards.tones()["scheduler"] == "var(--amber)"
    down = {**_HEALTH_OK, "scheduler": {"alive": False, "degraded": False}}
    cards(down, {"total": 0})
    assert cards.tones()["scheduler"] == "var(--red)"
