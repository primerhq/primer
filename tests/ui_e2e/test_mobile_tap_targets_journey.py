"""Journey: on a phone, a finished turn's controls are tap-sized and its text is readable (console review C-035).

At 390x844 the session screen drew its controls at desktop density: the header overflow button 28x28, the "rewind here" and "view trace" icons
16x16, a tool-call row 19px high, the composer's input 19px high, and the avatar initials, the usage label, the turn time and the lifecycle label
at 8.5 to 10.5px. The mobile floor in this stylesheet is ``--tap-min`` (44px; ``scripts/audit_touch_targets.py`` holds the rules of the
``max-width: 639px`` block to it), and 11px for text.

A real tool-calling turn is scripted through the mock LLM (the shape of ``test_trace_sidebar_journey``) so the screen has what a conversation has: a
user message, a tool call, an answer and a turn boundary. Every visible interactive element inside the mobile chat is then measured from
its bounding box, and every visible text node from its computed font size. Nothing is mocked in the console.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.mock_llm import Rule
from tests._support.smk import smk
from tests.ui_e2e.test_trace_sidebar_journey import _seed, _wait_for_turn_to_settle

pytestmark = smk("SMK-UI-06", status="partial")

PHONE = {"width": 390, "height": 844}
TAP_MIN = 44
TEXT_MIN = 11

_CONTROLS = """
() => {
  const root = document.querySelector('.nv-mobile-chat');
  const sel = 'button, a[href], input:not([type=hidden]), select, textarea, [role=button], [role=tab], [role=menuitem], summary';
  const out = [];
  for (const el of root.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    out.push({
      what: el.getAttribute('data-testid') || (el.className || el.tagName).toString().slice(0, 40),
      w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10,
    });
  }
  return out;
}
"""

_TEXT = """
() => {
  const root = document.querySelector('.nv-mobile-chat');
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const out = [];
  let n;
  while ((n = walker.nextNode())) {
    if (!n.nodeValue.trim()) continue;
    const el = n.parentElement;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const px = parseFloat(cs.fontSize);
    out.push({ what: (el.className || el.tagName).toString().slice(0, 40), text: n.nodeValue.trim().slice(0, 20), px });
  }
  return out;
}
"""


@pytest.fixture
def phone_session(page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path) -> Page:
    registry, mock_base_url = mock_llm_lan
    ids = _seed(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path)
    wid = ids["workspace"]
    registry.register(ids["model_name"], [
        Rule(when_tool_result=False, emit_tool="misc__uuid_v4", emit_args={}),
        Rule(when_tool_result=True, emit_text="Based on the generated id, done."),
    ])
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        r = client.post(f"/v1/workspaces/{wid}/sessions", json={
            "binding": {"kind": "agent", "agent_id": ids["agent"]},
            "initial_instructions": "mobile-tap-targets: use the tool then answer",
            "auto_start": True,
        })
        assert r.status_code == 201, f"create session failed: {r.status_code} {r.text}"
        _wait_for_turn_to_settle(client, r.json()["id"])
        sid = r.json()["id"]
    page.set_viewport_size(PHONE)
    page.goto(f"{console_url}#/w/{wid}?doc=session:{sid}")
    expect(page.get_by_text("Based on the generated id")).to_be_visible(timeout=20_000)
    expect(page.locator(".nv-mobile-chat")).to_be_visible()
    return page


@pytest.mark.ui_e2e
@pytest.mark.timeout(120)
def test_every_control_of_a_finished_turn_is_tap_sized(phone_session: Page) -> None:
    controls = phone_session.evaluate(_CONTROLS)
    assert len(controls) >= 6, f"the screen should have its header, turn and composer controls, found {controls}"
    small = [f"{c['what']} {c['w']}x{c['h']}" for c in controls if min(c["w"], c["h"]) < TAP_MIN]
    assert not small, f"controls under {TAP_MIN}px at 390px wide: {small}"


@pytest.mark.ui_e2e
@pytest.mark.timeout(120)
def test_no_text_of_a_finished_turn_is_under_11px(phone_session: Page) -> None:
    texts = phone_session.evaluate(_TEXT)
    assert len(texts) >= 5, f"the screen should have text to measure, found {texts}"
    small = sorted({f"{t['what']} '{t['text']}' {t['px']}px" for t in texts if t["px"] < TEXT_MIN})
    assert not small, f"text under {TEXT_MIN}px at 390px wide: {small}"
