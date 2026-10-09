"""The definition of "unnamed", on a page whose answer is known: the standing sweep can only pass if this can fail (console review C-003).

``tests/ui_e2e/_a11y.py`` enumerates the candidate controls of a page and takes each one's name from Chromium's accessibility tree. This page holds controls of every shape the earlier DOM
heuristic got wrong in either direction, and asserts the verdict on each by its ``data-testid`` (``unnamed-*`` must be flagged, ``named-*`` must not, ``skipped-*`` are not in the tree at all):

* named by Chromium, and by nothing the heuristic knew: ``title`` on an input or a select, an ``aria-labelledby`` target that is itself named by ``aria-label``, a label holding only an
  image with ``alt``, an image input, the default words of a submit and a reset;
* named ``""`` by Chromium although the heuristic passed them (``was passed before``): a label that is hidden four ways, a wrapped label whose text is ``aria-hidden``, a button whose text
  or icon is hidden five ways, a zero-width space, a checkbox with ``role=switch``, ``checkbox`` or ``radio`` whose ``value`` is "on", an icon-only link, a ``role=link`` with no name, a bare
  ``contenteditable`` editor, an empty ``listbox``, ``option``, ``menuitemcheckbox``, ``menuitemradio`` and ``treeitem``, a ``summary`` with no text, and a visually hidden (zero-size)
  checkbox that is still focusable.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page

from tests._support.smk import smk
from tests.ui_e2e._a11y import AxProbe

pytestmark = smk("SMK-UI-06", status="partial")

GIF = "data:image/gif;base64,R0lGODlhAQABAAAAACw="
SIZED = 'style="min-width:12px;min-height:12px;display:inline-block"'

_NAMED = f"""
<label for="n1">Name</label><input id="n1" data-testid="named-label-for">
<label>Wrapped <input data-testid="named-label-wrapping"></label>
<input aria-label="Search" data-testid="named-aria-label">
<span id="n2">Described by</span><input aria-labelledby="n2" data-testid="named-labelledby">
<select aria-label="Pick" data-testid="named-select"><option>a</option></select>
<textarea aria-label="Notes" data-testid="named-textarea"></textarea>
<button data-testid="named-text">Save</button>
<button aria-label="Close" data-testid="named-button-aria"><svg width="8" height="8"></svg></button>
<button title="Refresh" data-testid="named-button-title"><svg width="8" height="8"></svg></button>
<button data-testid="named-button-svg-title"><svg width="8" height="8"><title>Settings</title></svg></button>
<button data-testid="named-button-img-alt"><img alt="Logo" width="8" height="8" src="{GIF}"></button>
<div role="button" tabindex="0" data-testid="named-role-button">Open</div>
<label>Agree <input type="checkbox" data-testid="named-checkbox"></label>
<a href="#x" data-testid="named-link">Docs</a>
<div contenteditable="true" role="textbox" aria-label="Editor" {SIZED} data-testid="named-contenteditable"></div>
<details><summary data-testid="named-summary">More</summary>x</details>
<!-- named by Chromium, flagged by the earlier heuristic -->
<input title="Search" data-testid="named-input-title">
<select title="Kind" data-testid="named-select-title"><option>a</option></select>
<span id="alt1" aria-label="Lbl"></span><input aria-labelledby="alt1" data-testid="named-labelledby-target-aria-label">
<label for="li1"><img alt="Name" src="{GIF}" width="8" height="8"></label><input id="li1" data-testid="named-label-img-alt">
<input type="image" alt="Go" src="{GIF}" width="8" height="8" data-testid="named-input-image-alt">
<input type="submit" data-testid="named-input-submit-default">
<input type="reset" data-testid="named-input-reset-default">
<div role="combobox" title="Choose" {SIZED} data-testid="named-role-combobox-title"></div>
<input aria-labelledby="no-such-id3" aria-label="Fallback" data-testid="named-missing-labelledby-then-aria-label">
"""

_UNNAMED = f"""
<input placeholder="Filter things" data-testid="unnamed-placeholder-only">
<select data-testid="unnamed-select"><option value="">all kinds</option></select>
<textarea placeholder="Say something" data-testid="unnamed-textarea"></textarea>
<button data-testid="unnamed-icon-button"><svg width="8" height="8"></svg></button>
<label for="u1"></label><input id="u1" data-testid="unnamed-empty-label">
<button aria-label="" data-testid="unnamed-empty-aria-label"></button>
<div role="textbox" tabindex="0" {SIZED} data-testid="unnamed-role-textbox"></div>
<input type="checkbox" data-testid="unnamed-checkbox">
<div role="slider" tabindex="0" aria-valuenow="1" {SIZED} data-testid="unnamed-role-slider"></div>
<div role="tab" {SIZED} data-testid="unnamed-role-tab"></div>
<div role="menuitem" {SIZED} data-testid="unnamed-role-menuitem"></div>
<!-- empty in other ways -->
<button aria-label="   " data-testid="unnamed-aria-label-spaces"></button>
<button data-testid="unnamed-text-nbsp">&nbsp;&nbsp;</button>
<span id="emp"></span><input aria-labelledby="emp" data-testid="unnamed-labelledby-empty-target">
<input aria-labelledby="no-such-id" data-testid="unnamed-labelledby-missing-target">
<label for="el1">  </label><input id="el1" data-testid="unnamed-label-for-whitespace">
<!-- was passed before: Chromium names these "" -->
<button data-testid="unnamed-was-passed-zero-width-space">&#8203;</button>
<label for="hl1" style="display:none">Name</label><input id="hl1" data-testid="unnamed-was-passed-label-display-none">
<label for="hl2" hidden>Name</label><input id="hl2" data-testid="unnamed-was-passed-label-hidden-attr">
<label for="hl3" style="visibility:hidden">Name</label><input id="hl3" data-testid="unnamed-was-passed-label-visibility-hidden">
<label for="hl4" aria-hidden="true">Name</label><input id="hl4" data-testid="unnamed-was-passed-label-aria-hidden">
<label><span aria-hidden="true">Name</span><input data-testid="unnamed-was-passed-wrapped-label-text-aria-hidden"></label>
<button data-testid="unnamed-was-passed-text-aria-hidden"><span aria-hidden="true">x</span></button>
<button data-testid="unnamed-was-passed-text-display-none"><span style="display:none">Save</span><svg width="8" height="8"></svg></button>
<button data-testid="unnamed-was-passed-text-hidden-attr"><span hidden>Save</span><svg width="8" height="8"></svg></button>
<button data-testid="unnamed-was-passed-text-visibility-hidden"><span style="visibility:hidden">Save</span></button>
<button data-testid="unnamed-was-passed-svg-title-aria-hidden"><svg aria-hidden="true" width="8" height="8"><title>Settings</title></svg></button>
<button data-testid="unnamed-was-passed-img-alt-display-none"><img alt="Logo" style="display:none" src="{GIF}"><svg width="8" height="8"></svg></button>
<button data-testid="unnamed-was-passed-style-element-only"><style>.zz{{color:red}}</style><svg width="8" height="8"></svg></button>
<input type="checkbox" role="switch" data-testid="unnamed-was-passed-checkbox-role-switch">
<input type="checkbox" role="checkbox" data-testid="unnamed-was-passed-checkbox-role-checkbox">
<input type="radio" role="radio" data-testid="unnamed-was-passed-radio-role-radio">
<a href="#x" data-testid="unnamed-was-passed-link-icon-only"><svg width="8" height="8"></svg></a>
<div role="link" tabindex="0" {SIZED} data-testid="unnamed-was-passed-role-link"></div>
<div contenteditable="true" {SIZED} data-testid="unnamed-was-passed-contenteditable"></div>
<div role="listbox" {SIZED} data-testid="unnamed-was-passed-role-listbox"><div role="option" {SIZED} data-testid="unnamed-was-passed-role-option"></div></div>
<div role="menu" {SIZED}><div role="menuitemcheckbox" {SIZED} data-testid="unnamed-was-passed-role-menuitemcheckbox"></div><div role="menuitemradio" {SIZED} data-testid="unnamed-was-passed-role-menuitemradio"></div></div>
<div role="tree" {SIZED}><div role="treeitem" {SIZED} data-testid="unnamed-was-passed-role-treeitem"></div></div>
<details><summary data-testid="unnamed-was-passed-summary-empty"></summary>x</details>
<input type="checkbox" style="position:absolute;opacity:0;width:0;height:0" data-testid="unnamed-was-passed-zero-size-focusable-checkbox">
"""

_NOT_EXPOSED = """
<input type="hidden" data-testid="skipped-hidden-input">
<input placeholder="x" style="display:none" data-testid="skipped-display-none">
<input placeholder="x" style="visibility:hidden" data-testid="skipped-visibility-hidden">
<div aria-hidden="true"><input placeholder="x" data-testid="skipped-aria-hidden"></div>
<div inert><input placeholder="x" data-testid="skipped-inert"></div>
<div style="display:none"><button data-testid="skipped-inside-display-none"></button></div>
"""


def _testids(html: str, prefix: str) -> set[str]:
    return set(re.findall(rf'data-testid="({prefix}[a-z0-9-]*)"', html))


@pytest.mark.ui_e2e
def test_the_definition_of_unnamed_flags_exactly_what_chromium_does_not_name(page: Page) -> None:
    page.set_content(f"<main>{_NAMED}{_UNNAMED}{_NOT_EXPOSED}</main>")
    probe = AxProbe(page)
    try:
        result = probe.examine("main")
    finally:
        probe.close()
    flagged = {item["testid"] for item in result.unnamed}
    expected = _testids(_UNNAMED, "unnamed-")
    assert flagged == expected, ("flagged but named:", sorted(flagged - expected), "named by nobody and not flagged:", sorted(expected - flagged))
    assert result.examined == len(_testids(_NAMED, "named-")) + len(expected), "every named and unnamed shape was examined, and nothing else"
    assert len(expected) >= 40, "the known-answer page shrank"


@pytest.mark.ui_e2e
def test_a_placeholder_alone_is_not_a_name_but_a_title_or_a_label_beside_it_is(page: Page) -> None:
    page.set_content("""<main>
<input placeholder="Filter" data-testid="unnamed-placeholder-only">
<input placeholder="Filter" title="Filter things" data-testid="named-placeholder-and-title">
<label for="a">Filter</label><input id="a" placeholder="e.g. foo" data-testid="named-placeholder-and-label">
<input placeholder="Filter" aria-label="" data-testid="unnamed-placeholder-and-empty-aria-label">
</main>""")
    probe = AxProbe(page)
    try:
        flagged = {item["testid"]: item["role"] for item in probe.examine("main").unnamed}
    finally:
        probe.close()
    assert set(flagged) == {"unnamed-placeholder-only", "unnamed-placeholder-and-empty-aria-label"}, flagged


@pytest.mark.ui_e2e
def test_a_root_that_matches_nothing_visible_is_an_error_and_not_the_whole_page(page: Page) -> None:
    page.set_content('<div class="modal" style="display:none"></div><input placeholder="x" data-testid="outside">')
    probe = AxProbe(page)
    try:
        with pytest.raises(AssertionError, match="matches no visible element"):
            probe.examine(".modal")
        with pytest.raises(AssertionError, match="matches no visible element"):
            probe.examine(".no-such-root")
    finally:
        probe.close()


@pytest.mark.ui_e2e
def test_every_visible_root_is_swept_not_only_the_first(page: Page) -> None:
    page.set_content('<div class="modal" style="display:none"></div><div class="modal"><input placeholder="x" data-testid="in-second-modal"></div>')
    probe = AxProbe(page)
    try:
        found = probe.examine(".modal")
    finally:
        probe.close()
    assert [item["testid"] for item in found.unnamed] == ["in-second-modal"]
