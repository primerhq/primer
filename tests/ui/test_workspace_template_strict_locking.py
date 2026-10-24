"""The workspace template overlay sets ``strict_write_locking`` (``WorkspaceTemplate.strict_write_locking``, Task 9 of the write-locking work; design 14.2).

The old form drew it as a ``role="switch"`` toggle (``WT_Toggle``); the old form went with Plan 08 Task 15, and the overlay draws it as the kit's two-way
choice in its Advanced fold: "Strict write locking", ``Per folder`` (off, the default) or ``Whole workspace`` (on). ``WT_formFromTemplate`` reads the stored
value, and ``WT_templateBody`` writes it when the person changed it (and keeps the stored one when they did not).

The overlay renders on its own harness (``tests/ui/test_wt_template_overlay.py``); the pure helpers run next to it in the same context.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._register_harness import base_code, context, js, settle, transpiled
from tests.ui.test_wt_helpers import CHOICES, LOCAL_ROW
from tests.ui.test_wt_template_overlay import SHARED_FILE, STUBS, TEMPLATES_FILE

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"


@pytest.fixture(scope="module")
def code() -> str:
    return base_code() + "\n" + transpiled(SHARED_FILE, TEMPLATES_FILE)


@pytest.fixture
def ctx(code: str):
    c = context(code)
    c.eval(STUBS)
    c.eval("serve('ws:provider-choices', { items: " + json.dumps([CHOICES["loc"]]) + " });")
    try:
        yield c
    finally:
        c.close()


def _mount(ctx, props: str) -> None:
    ctx.eval("MR.mount(function Nothing() { return null; }, {});")
    ctx.eval("ROLE = 'admin'; MR.mount(window.WT_TemplateOverlay, Object.assign({ onClose: onClose, onSaved: onSaved }, " + props + "));")
    ctx.eval("MR.click('wt-advanced:toggle');")


def test_the_choice_is_the_kits_two_way_control_in_the_advanced_fold(ctx) -> None:
    _mount(ctx, "{ create: true }")
    control = "MR.findProp('testid', 'ws-template-strict-write-locking')"
    assert ctx.eval(control + ".type === window.PK_Segmented") is True
    assert js(ctx, control + ".props.options") == [{"value": "off", "label": "Per folder"}, {"value": "on", "label": "Whole workspace"}]
    assert js(ctx, control + ".props.value") == "off", "a new template locks per folder"
    assert ctx.eval("textOf(MR.find('wt-advanced'))").count("Strict write locking") == 1


def test_a_new_template_sends_it_off_unless_the_person_turns_it_on(ctx) -> None:
    _mount(ctx, "{ create: true }")
    ctx.eval("setValue('ws-template-description', 'd'); press('wt-save');")
    settle(ctx)
    assert [c["body"]["strict_write_locking"] for c in js(ctx, "CALLS") if c["method"] == "POST"] == [False]
    _mount(ctx, "{ create: true }")
    ctx.eval("CALLS = []; MR.click('ws-template-strict-write-locking:on'); setValue('ws-template-description', 'd'); press('wt-save');")
    settle(ctx)
    assert [c["body"]["strict_write_locking"] for c in js(ctx, "CALLS") if c["method"] == "POST"] == [True]


def test_an_edit_shows_the_stored_value_and_writes_the_change(ctx) -> None:
    row = {**LOCAL_ROW, "strict_write_locking": True}
    ctx.eval(f"ROWS[{json.dumps('/workspace_templates/' + row['id'])}] = {json.dumps(row)};")
    _mount(ctx, "{ row: " + json.dumps(row) + " }")
    assert js(ctx, "MR.findProp('testid', 'ws-template-strict-write-locking').props.value") == "on"
    ctx.eval("MR.click('ws-template-strict-write-locking:off'); press('wt-save');")
    settle(ctx)
    assert [c["body"] for c in js(ctx, "CALLS") if c["method"] == "PUT"] == [{**row, "strict_write_locking": False}]


def test_the_helpers_read_and_write_it(ctx) -> None:
    assert js(ctx, "WT_formFromTemplate(" + json.dumps({**LOCAL_ROW, "strict_write_locking": True}) + ").strict") == "on"
    assert js(ctx, "WT_formFromTemplate(" + json.dumps({**LOCAL_ROW, "strict_write_locking": False}) + ").strict") == "off"
    assert js(ctx, "WT_blankForm().strict") == "off"
    body = js(ctx, "(function () { var row = " + json.dumps(LOCAL_ROW) + "; var form = WT_formFromTemplate(row); var initial = JSON.parse(JSON.stringify(form));"
                   " form.strict = 'on'; return WT_templateBody(row, form, initial); })()")
    assert body == {**LOCAL_ROW, "strict_write_locking": True}


def test_bundle_transpiles_with_templates() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/workspaces/templates.jsx === */" in text
