"""G13 fix C (wizard/docs batch): the audit findings G13-06, G13-07 and G13-08
from the G13 sweep (/var/tmp/revamp/audit/G13.md), asserted from the committed
sources.

Red-first contract (the brief): at the red commit these tests assert the
post-fix state and FAIL; at head they are green.

- G13-06: step 2 of the setup wizard offers a way back to step 1 (the
  "Change provider" row), so a wrong type or Base URL is correctable from
  the wizard; the saved row is kept for SW_saveProvider's idempotent update.
- G13-07: the setup waiting screen's promise ("works as soon as they
  finish") is kept by a slow re-read of /auth/status while setup is
  incomplete and the viewer is not the configuring admin; the one-shot
  login/register shape and the failure-retry cadence are untouched.
- G13-08: ui-foundation.md describes the real mobile architecture (the
  console root mounts NV_MobileShell on the mobile band) instead of denying
  the separate mobile surface the code mounts.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIZARD = ROOT / "ui" / "components" / "setup-wizard.jsx"
AUTH = ROOT / "ui" / "components" / "auth.jsx"
FOUNDATION_DOC = ROOT / "docs" / "dev" / "subsystems" / "ui-foundation.md"


def _wizard() -> str:
    return WIZARD.read_text(encoding="utf-8")


def _auth() -> str:
    return AUTH.read_text(encoding="utf-8")


def _auth_gate(src: str) -> str:
    m = re.search(r"function AuthGate\(\{ children \}\) \{.*?return children;\n  \}", src, re.S)
    assert m, "AuthGate not found in auth.jsx"
    return m.group(0)


def _step_two_block(src: str) -> str:
    m = re.search(r"\{step === 2 && \(.*?\n      \)\}", src, re.S)
    assert m, "the step-2 block of SetupWizardSteps not found"
    return m.group(0)


# ---------------------------------------------------------------------------
# G13-06 - step 2 offers a way back to step 1
# ---------------------------------------------------------------------------

def test_step_two_offers_a_change_provider_row() -> None:
    """Once step 1's probe succeeds the row is SAVED and step 2 was the only
    screen the wizard offered: a wrong type or Base URL could not be
    corrected from the wizard (a reload re-enters at step 2). The row runs
    setStep(1) and keeps the saved row for the idempotent re-save."""
    block = _step_two_block(_wizard())
    m = re.search(r"<button[^>]*data-testid=\"setup-change-provider\"[^>]*>(.*?)</button>", block, re.S)
    assert m, "step 2 offers no way back to step 1"
    assert "Change provider" in m.group(1)
    assert "setStep(1)" in block


def test_the_change_provider_row_writes_nothing() -> None:
    """Going back re-probes step 1; the row itself must not write (no
    provider delete or other request): the re-save is SW_saveProvider's
    idempotent create-or-update when step 1 is submitted again."""
    src = _wizard()
    block = _step_two_block(src)
    m = re.search(r"<button[^>]*data-testid=\"setup-change-provider\"[^>]*>", block)
    assert m, "step 2 offers no way back"
    tag = m.group(0)
    assert "apiFetch" not in tag and "DELETE" not in src


def test_the_resume_shape_is_unchanged() -> None:
    """The fix must not move the resume decision: a saved provider without a
    profile still re-enters at step 2 (run for real in V8, as test_setup_wizard.py
    runs the same helpers)."""
    import json

    from py_mini_racer import MiniRacer

    src = _wizard()
    start = src.index("function SW_providerId(")
    end = src.index("// ---- end of the resume helpers")
    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval((ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8"))
    ctx.eval(src[start:end])
    plan = json.loads(ctx.eval(
        "JSON.stringify(SW_resumePlan([{id: 'llm-openchat', provider: 'openchat'}], []))"
    ))
    assert plan == {"step": 2, "providerId": "llm-openchat"}


# ---------------------------------------------------------------------------
# G13-07 - the waiting screen's promise is kept by a slow re-read
# ---------------------------------------------------------------------------

def test_the_waiting_viewer_re_reads_the_auth_status_on_a_slow_cadence() -> None:
    """A successful read that says setup is incomplete was never re-read
    while the tab stayed open, so the waiting user sat on the screen until a
    manual reload. The gate's own read stays one-shot (the neighbouring order
    tests pin its source shape), so the WAITING SCREEN owns the wait: a slow
    re-read of /auth/status, and a hand back to the host (onDone, a reload)
    when setup is no longer incomplete."""
    wiz = _wizard()
    gate = _auth_gate(_auth())
    assert "AUTH_SETUP_WAIT_MS" in wiz, "no slow waiting cadence constant"
    assert "AUTH_SETUP_WAIT_MS" not in gate, "the poll must not move into the gate"
    m = re.search(r"function SetupWaitingScreen\(\{ username, onDone \}\) \{(.*?)^\}", wiz, re.S | re.M)
    assert m, "SetupWaitingScreen no longer takes onDone"
    body = m.group(1)
    assert '"/auth/status"' in body, "the waiting screen does not re-read the status"
    assert "setTimeout(ask, AUTH_SETUP_WAIT_MS)" in body
    assert "status.setup_complete !== false" in body, "the re-read is not guarded on setup"
    assert "onDone()" in body, "the re-read does not hand the tab back to the host"
    assert "window.location.reload()" in _auth(), "the host's hand-back is not a reload"
    assert 'onDone={() => window.location.reload()}' in gate, "the gate does not pass onDone"


def test_the_failure_retry_cadence_is_unchanged() -> None:
    """The one retry timer on a FAILED read (AUTH_RETRY_MS, the catch
    branch) keeps its shape: the waiting re-read lives in the waiting
    screen, not the gate."""
    gate = _auth_gate(_auth())
    assert "AUTH_RETRY_MS" in gate
    assert "setTimeout(() => setAttempt((n) => n + 1), AUTH_RETRY_MS)" in gate


def test_login_register_keep_the_one_shot_shape() -> None:
    """AuthGate still reads /auth/status once per attempt (the waiting
    screen's re-read is its own), the gate's source shape the neighbouring
    order tests pin is untouched, and the waiting copy is the promise the
    poll now keeps - the FALLBACK copy change is not the path taken."""
    gate = _auth_gate(_auth())
    assert gate.count('"/auth/status"') == 1
    assert gate.index("must_change_password") < gate.index("status.setup_complete")
    assert gate.index("status.setup_complete") < gate.index("return children")
    assert "setup_complete: false" not in gate
    assert "works as soon as they finish" in _wizard()


# ---------------------------------------------------------------------------
# G13-08 - the dev doc describes the real mobile architecture
# ---------------------------------------------------------------------------

def test_the_foundation_doc_describes_the_mobile_swap() -> None:
    """The sentence that denied the separate mobile surface (rewritten at
    cc7acf764) is replaced by one that describes the swap the code does:
    the console root mounts NV_MobileShell on the mobile band, and the
    true parts (one bundle, useViewport as the shared breakpoint) stay."""
    doc = FOUNDATION_DOC.read_text(encoding="utf-8")
    assert "rather than swapping in a separate mobile surface" not in doc, \
        "the false claim is still in ui-foundation.md"
    assert "NV_MobileShell" in doc
    assert "mobile band" in doc
    assert "Single bundle" in doc, "the true single-bundle part was lost"
    assert "\u2014" not in doc, "em-dash in docs/dev (the hygiene suite forbids it)"
