"""The standing a11y sweep visits every page the shell has, and every form says how it opens (console review C-003, review of #668).

``tests/ui_e2e/_a11y_surfaces.py`` is the list of what ``tests/ui_e2e/test_console_controls_have_names_sweep.py`` opens. A browser run can only find unnamed controls on a page it opens, so the list is
held here to the console's own: a Platform or System view added to ``ui/foundation/shell-url.js`` without a row fails this file, a row for a view that no longer exists fails it, and every page
with no create form says why in ``NO_FORM``. (The old sweep opened only the legacy overlays and 9 of 24 create forms, and never the views the Platform nav opens.)
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.ui_e2e import _a11y_surfaces as surfaces

ROOT = Path(__file__).resolve().parents[2]
SHELL_URL = (ROOT / "ui" / "foundation" / "shell-url.js").read_text(encoding="utf-8")
ROUTER_HELPERS = (ROOT / "tests" / "ui_e2e" / "_shell_helpers.py").read_text(encoding="utf-8")


def _grammar(name: str) -> list[str]:
    block = re.search(rf"\b{name}:\s*\[([^\]]*)\]", SHELL_URL[SHELL_URL.index("var SH_VIEWS"):])
    assert block, f"SH_VIEWS.{name} not found in shell-url.js"
    return re.findall(r'"([a-z_]+)"', block.group(1))


def test_the_grammar_is_read_as_the_shell_defines_it() -> None:
    assert "providers" in _grammar("platform") and "dashboard" in _grammar("system")


def test_every_platform_view_of_the_shell_is_swept_and_none_that_does_not_exist() -> None:
    assert sorted(surfaces.PLATFORM_FORMS) == sorted(_grammar("platform"))


def test_every_system_view_of_the_shell_is_swept() -> None:
    assert sorted(surfaces.SYSTEM_VIEWS) == sorted(_grammar("system"))


def test_a_page_with_no_form_says_why() -> None:
    no_form = {f"overlay-page {name}" for name, forms in surfaces.LEGACY_FORMS.items() if not forms}
    no_form |= {f"platform-view {name}" for name, forms in surfaces.PLATFORM_FORMS.items() if not forms}
    assert no_form == set(surfaces.NO_FORM), (sorted(no_form), sorted(surfaces.NO_FORM))
    for name, why in surfaces.NO_FORM.items():
        assert len(why.split()) >= 4, f"{name}: say why it has no create form"


def test_every_form_has_a_button_and_a_root_that_the_sweep_waits_for() -> None:
    for table in (surfaces.LEGACY_FORMS, surfaces.PLATFORM_FORMS):
        for page, forms in table.items():
            for button, root in forms:
                assert button.strip() and root.strip(), (page, button, root)
                assert root in {surfaces.MODAL, surfaces.OVERLAY, surfaces.PLATFORM, surfaces.NEW_WORKSPACE_OVERLAY}, (page, root)


def test_the_legacy_routes_are_the_ones_the_shell_helper_can_open() -> None:
    for route in surfaces.LEGACY_FORMS:
        assert route.split("/")[0] in {"agents", "graphs", "triggers", "toolsets", "approvals", "workers", "harnesses", "services", "workspaces", "channels", "knowledge",
                                       "subsystems", "providers"}, route


def test_the_expected_visit_list_has_no_duplicates_and_covers_each_kind_of_surface() -> None:
    names = surfaces.expected_surfaces(7)
    assert len(names) == len(set(names)), "a surface is recorded under one name"
    kinds = {n.split(" ")[0] for n in names}
    assert {"studio", "session", "overlay", "overlay-page", "platform-view", "system-view", "graph", "phone"} <= kinds
    assert sum(n.startswith("graph builder / step ") for n in names) == 7
    assert names[:2] == ["studio", "session document"]


def test_every_form_is_in_the_visit_list_under_its_button() -> None:
    names = set(surfaces.expected_surfaces(1))
    for route, forms in surfaces.LEGACY_FORMS.items():
        for button, _root in forms:
            assert surfaces.form_surface("overlay-page", route, button) in names
    for nav, forms in surfaces.PLATFORM_FORMS.items():
        for button, _root in forms:
            assert surfaces.form_surface("platform-view", nav, button) in names


def test_the_phone_has_its_four_tabs() -> None:
    assert surfaces.PHONE_TABS == ["Inbox", "Spaces", "Files", "More"]
