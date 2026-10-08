"""``SetupWizardGate`` mounted for real: what it draws after step 2, after a failed seed, and after a failed read (nits of the review of #612).

The two fixes of #612 were pinned only by browser journeys: the gate held the setup state it read BEFORE the wizard ran and drew its red failure right after the operator had fixed it, and
a failed seed left the checklist showing whatever was read last. Neither can be reached by a mutant of the source in a unit test unless the real component runs. Here it does, in V8
through ``tests/ui/_mini_react.py``, with a scripted ``primerApi.apiFetch`` (a queue of ``GET /setup/state`` answers, and a ``POST /setup/seed`` the test settles by hand) and the two
children stubbed (``SetupWizardSteps`` hands the test its ``onComplete``; ``SetupPredicatesList`` shows the state it was given).

A failed SEED also used the read's banner title, "Couldn't load setup state", which is not what failed: it has its own title now.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "setup-wizard.jsx").read_text(encoding="utf-8")

LOAD_TITLE = "Couldn't load setup state"
SEED_TITLE = "Couldn't run the setup seed"

_PRELUDE = r"""
var __log = [];
var __states = [];
var __seed = null;
window.primerApi = {
  apiFetch: function (method, path) {
    __log.push(method + " " + path);
    if (path === "/setup/state") {
      var next = __states.shift();
      return next && next.__reject ? Promise.reject(next.__reject) : Promise.resolve(next);
    }
    if (path === "/setup/seed") return new Promise(function (resolve, reject) { __seed = { resolve: resolve, reject: reject }; });
    return Promise.reject(new Error("unexpected " + method + " " + path));
  },
};
function SetupWizardSteps(props) { window.__complete = props.onComplete; return React.createElement("div", { "data-testid": "wizard-steps" }, "steps"); }
function SetupPredicatesList(props) {
  window.__rerun = props.onRerunSeed;
  return React.createElement("div", { "data-testid": "pred-list", "data-complete": String(props.state.complete), "data-marker": props.state.marker }, "predicates");
}
function __flush() { MR.rerender(); MR.rerender(); MR.rerender(); }
"""


def _state(*, complete: bool, provider_ok: bool, marker: str) -> dict:
    return {"complete": complete, "marker": marker, "predicates": [{"key": "llm_provider", "ok": provider_ok}, {"key": "model_profile", "ok": provider_ok}]}


FRESH = _state(complete=False, provider_ok=False, marker="before-the-wizard")
DONE = _state(complete=True, provider_ok=True, marker="after-the-seed")
HALF = _state(complete=False, provider_ok=True, marker="after-a-failed-seed")


@functools.lru_cache(maxsize=1)
def _compiled() -> str:
    from primer.api._jsx_bundle import JSXBundler

    def fn(name: str) -> str:
        start = SRC.index("function " + name + "(")
        return SRC[start:SRC.index("\n}\n", start) + len("\n}\n")]

    ui = ROOT / "ui"
    bundler = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    try:
        return bundler._transform("\n".join(fn(n) for n in ("SW_tidy", "SW_errorText", "_fetchSetupState", "SetupWizardGate")), "snippet.jsx")
    finally:
        bundler._ctx.close()


class Gate:
    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def flush(self) -> None:
        self.ctx.eval("__flush()")

    def texts(self) -> str:
        return self.ctx.eval("MR.texts().join(' | ')")

    def prop(self, testid: str, name: str):
        return self.ctx.eval(f"(function () {{ var e = MR.find({json.dumps(testid)}); return e ? e.props[{json.dumps(name)}] : null; }})()")

    def drawn(self, testid: str) -> bool:
        return bool(self.ctx.eval(f"MR.find({json.dumps(testid)}) !== null"))

    def log(self) -> list[str]:
        return json.loads(self.ctx.eval("JSON.stringify(__log)"))

    def finish_step_two(self) -> None:
        """The operator finishes the provider and model steps: the wizard hands control back."""
        self.ctx.eval("window.__complete()")
        self.flush()

    def settle_seed(self, how: str, error: dict | None = None) -> None:
        self.ctx.eval(f"__seed.{how}({json.dumps(error)})" if how == "reject" else "__seed.resolve(null)")
        self.flush()


@pytest.fixture
def gate():
    made = []

    def mount(*states: dict) -> Gate:
        ctx = mini_react_context(_compiled(), _PRELUDE)
        made.append(ctx)
        ctx.eval("__states = " + json.dumps(list(states)))
        ctx.eval("MR.mount(SetupWizardGate, { onDone: function () {} })")
        g = Gate(ctx)
        g.flush()
        return g

    try:
        yield mount
    finally:
        for c in made:
            c.close()


def test_a_fresh_install_lands_on_the_two_step_form(gate) -> None:
    g = gate(FRESH)
    assert g.drawn("wizard-steps") and not g.drawn("pred-list")


def test_finishing_step_two_draws_no_state_until_the_seed_has_been_read_again(gate) -> None:
    """The state in hand says no provider is configured; drawing it would show a red failure right after the operator fixed it (console review C-039)."""
    g = gate(FRESH, DONE)
    g.finish_step_two()
    assert g.log() == ["GET /setup/state", "POST /setup/seed"], g.log()
    assert "Loading" in g.texts()
    assert not g.drawn("pred-list"), "the pre-wizard state must not be drawn"
    assert g.prop("setup-gate-enter", "disabled") is True, "Enter stays off while there is no state"


def test_once_the_seed_has_run_the_fresh_state_is_read_and_drawn_and_enter_opens(gate) -> None:
    g = gate(FRESH, DONE)
    g.finish_step_two()
    g.settle_seed("resolve")
    assert g.log() == ["GET /setup/state", "POST /setup/seed", "GET /setup/state"]
    assert g.prop("pred-list", "data-marker") == "after-the-seed"
    assert g.prop("setup-gate-enter", "disabled") is False


def test_a_failed_seed_re_reads_the_state_and_says_the_seed_failed_not_that_the_state_did(gate) -> None:
    """The checklist shows what is true after the failed seed (the re-read), with its own banner: "Couldn't load setup state" was the title of a different failure."""
    g = gate(FRESH, HALF)
    g.finish_step_two()
    g.settle_seed("reject", {"message": "the workspace backend refused the root path"})
    assert g.log() == ["GET /setup/state", "POST /setup/seed", "GET /setup/state"], "a failed seed still re-reads the state"
    assert g.prop("pred-list", "data-marker") == "after-a-failed-seed"
    shown = g.texts()
    assert SEED_TITLE in shown and "the workspace backend refused the root path" in shown, shown
    assert LOAD_TITLE not in shown, "the seed's failure is not a failed read"


def test_a_failed_read_keeps_its_own_title_and_draws_no_state(gate) -> None:
    g = gate({"__reject": {"message": "connection refused"}})
    shown = g.texts()
    assert LOAD_TITLE in shown and "connection refused" in shown, shown
    assert SEED_TITLE not in shown
    assert not g.drawn("pred-list") and "Loading" not in shown


def test_a_good_re_run_of_the_seed_clears_the_old_failure(gate) -> None:
    g = gate(FRESH, HALF, DONE)
    g.finish_step_two()
    g.settle_seed("reject", {"message": "boom"})
    assert SEED_TITLE in g.texts()

    g.ctx.eval("window.__rerun()")
    g.flush()
    g.settle_seed("resolve")

    shown = g.texts()
    assert SEED_TITLE not in shown and LOAD_TITLE not in shown, shown
    assert g.prop("pred-list", "data-marker") == "after-the-seed"
    assert g.prop("setup-gate-enter", "disabled") is False
