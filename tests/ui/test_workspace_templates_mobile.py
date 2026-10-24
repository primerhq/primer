"""Platform › Workspace templates on a phone: the page reflows through the kit, with no phone branch of its own.

The old list (``WorkspaceTemplatesPage``, removed by Plan 08 Task 15) branched on ``useViewport``: a ``CardList`` and a "New template" ``Fab`` on a phone.
The page that replaced it (``WT_TemplatesPage``) is the kit's: its cards are a ``PK_CardGrid`` (``.pk-grid``, an auto-fill grid that drops to one
column on a narrow pane), "+ Template" is the kit header's action, and the template opens in ``PK_FormOverlay``, the shared ``Modal`` that is a bottom
sheet on a phone. The page renders on its own harness (``tests/ui/test_wt_templates_page.py``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._register_harness import base_code, context, js, transpiled
from tests.ui.test_wt_helpers import CHOICES
from tests.ui.test_wt_template_overlay import SHARED_FILE, STUBS, TEMPLATES_FILE
from tests.ui.test_wt_templates_page import PAGE_STUBS, PLATFORM, PYTHON_DEV, _fn, _mount

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "workspaces" / "templates.jsx").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")
SHARED = (ROOT / "ui" / "components" / "shared.jsx").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def code() -> str:
    """The page's context (``tests/ui/test_wt_templates_page.py``): the overlay's files and the real count and empty texts of ``nv-platform.jsx``."""
    helpers = "\n".join(_fn(PLATFORM, n) for n in ("NV_emptyText", "NV_countText", "NV_nounOf"))
    exports = "\nwindow.NV_emptyText = NV_emptyText; window.NV_countText = NV_countText;\n"
    return base_code() + "\n" + transpiled(SHARED_FILE, TEMPLATES_FILE) + "\n" + helpers + exports


@pytest.fixture
def ctx(code: str):
    c = context(code)
    c.eval(STUBS)
    c.eval(PAGE_STUBS)
    c.eval("serve('ws:provider-choices', { items: " + json.dumps([CHOICES["dock"], CHOICES["k3s"], CHOICES["loc"]]) + " });")
    try:
        yield c
    finally:
        c.close()


def test_no_phone_branch_of_its_own() -> None:
    for gone in ("CardList", "Fab", "useViewport", "isMobile"):
        assert not re.search(rf"\b{gone}\b", SRC), gone


def test_the_cards_are_the_kits_reflowing_grid(ctx) -> None:
    _mount(ctx, [PYTHON_DEV])
    grid = js(ctx, "MR.find('wt-grid').props.className")
    assert grid == "pk-grid"
    assert ctx.eval("!!MR.find('pk-card:python-dev')")
    body = CSS[CSS.index(".pk-grid {"):]
    body = body[:body.index("}")]
    assert "grid-template-columns: repeat(auto-fill, minmax(" in body, "an auto-fill grid drops to one column on a phone"


def test_plus_template_opens_the_kit_form_overlay(ctx) -> None:
    _mount(ctx, [PYTHON_DEV])
    assert js(ctx, "HEADER.action.label") == "+ Template" and js(ctx, "HEADER.action.testid") == "wt-new"
    ctx.eval("MR.click('wt-new');")
    assert js(ctx, "LAST_OVERLAY.title") == "New workspace template" and js(ctx, "LAST_OVERLAY.testid") == "wt-overlay"
    # PK_FormOverlay is the shared Modal, which is a bottom sheet on a phone.
    assert "useViewport" in SHARED and "sheet-overlay" in SHARED
