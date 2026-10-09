"""The standing a11y sweep visits every page the shell has, every form says how it opens, and every surface says how it knows it got there (console review C-003, review of #668).

``tests/ui_e2e/_a11y_surfaces.py`` is the list of what ``tests/ui_e2e/test_console_controls_have_names_sweep.py`` opens. A browser run can only find unnamed controls on a page it opens, so the list is
held here to the console's own: a Platform or System view added to ``ui/foundation/shell-url.js`` without a row fails this file, a row for a view that no longer exists fails it, every page
with no create form says why in ``NO_FORM``, and every Platform create button the console draws has a form entry (the old sweep opened only the legacy overlays and 9 of 24 create forms, never the
views the Platform nav opens, clicked a dropdown toggle where it meant to open the provider form, and passed on a page that was only its own chrome).
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.ui_e2e import _a11y_surfaces as surfaces
from tests.ui_e2e._shell_helpers import overlay_target

ROOT = Path(__file__).resolve().parents[2]
SHELL_URL = (ROOT / "ui" / "foundation" / "shell-url.js").read_text(encoding="utf-8")
PLATFORM_JSX = (ROOT / "ui" / "components" / "console" / "nv-platform.jsx").read_text(encoding="utf-8")
SYSTEM_JSX = (ROOT / "ui" / "components" / "console" / "nv-system.jsx").read_text(encoding="utf-8")
OVERLAYS_JSX = (ROOT / "ui" / "components" / "console" / "nv-overlays.jsx").read_text(encoding="utf-8")
MOBILE_JSX = (ROOT / "ui" / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")
CATALOG_JSX = (ROOT / "ui" / "components" / "provider-catalog.jsx").read_text(encoding="utf-8")


def _grammar(name: str) -> list[str]:
    block = re.search(rf"\b{name}:\s*\[([^\]]*)\]", SHELL_URL[SHELL_URL.index("var SH_VIEWS"):])
    assert block, f"SH_VIEWS.{name} not found in shell-url.js"
    return re.findall(r'"([^"]+)"', block.group(1))


def _plat_pages() -> dict[str, str]:
    """``NV_PLAT_PAGES``: the source of each page's entry, by id."""
    body = PLATFORM_JSX[PLATFORM_JSX.index("var NV_PLAT_PAGES = {"):]
    body = body[: body.index("\n};")]
    keys = list(re.finditer(r"^  (\w+): \{$", body, re.M))
    assert keys, "NV_PLAT_PAGES has no entries"
    return {m.group(1): body[m.end(): keys[i + 1].start() if i + 1 < len(keys) else len(body)] for i, m in enumerate(keys)}


def _titles(name: str) -> dict[str, str]:
    block = re.search(rf"var {name} = \{{(.*?)\n\}};", OVERLAYS_JSX, re.S)
    assert block, f"{name} not found in nv-overlays.jsx"
    return {key.strip('"'): value for key, value in re.findall(r'^\s*("?[\w:-]+"?):\s*"([^"]+)"', block.group(1), re.M)}


def test_the_grammar_is_read_as_the_shell_defines_it() -> None:
    assert "providers" in _grammar("platform") and "dashboard" in _grammar("system")


def test_the_grammar_reads_any_quoted_id_not_only_lowercase_words() -> None:
    assert re.findall(r'"([^"]+)"', 'a: ["x-y", "Z9", "model_profile"]') == ["x-y", "Z9", "model_profile"]


def test_every_platform_view_of_the_shell_is_swept_and_none_that_does_not_exist() -> None:
    assert sorted(surfaces.PLATFORM_FORMS) == sorted(_grammar("platform"))


def test_every_system_view_of_the_shell_is_swept() -> None:
    assert sorted(surfaces.SYSTEM_VIEWS) == sorted(_grammar("system"))
    assert sorted(surfaces.SYSTEM_FORMS) == sorted(set(surfaces.SYSTEM_FORMS) & set(surfaces.SYSTEM_VIEWS)), "a form entry for a view that does not exist"


def test_a_page_with_no_form_says_why() -> None:
    no_form = {f"overlay-page {name}" for name, forms in surfaces.LEGACY_FORMS.items() if not forms}
    no_form |= {f"platform-view {name}" for name, forms in surfaces.PLATFORM_FORMS.items() if not forms}
    no_form |= {f"system-view {name}" for name in surfaces.SYSTEM_VIEWS if not surfaces.SYSTEM_FORMS.get(name)}
    assert no_form == set(surfaces.NO_FORM), (sorted(no_form), sorted(surfaces.NO_FORM))
    for name, why in surfaces.NO_FORM.items():
        assert len(why.split()) >= 4, f"{name}: say why it has no create form"


def test_the_system_views_that_open_a_create_form_are_swept_through_it() -> None:
    """The reviewer's list (B2'): Create user, Add provider (SSO) and, through the embedded token page, Create token."""
    assert surfaces.SYSTEM_FORMS == {
        "users": [surfaces.Form("Create user", surfaces.MODAL)],
        "sso": [surfaces.Form("Add provider", surfaces.MODAL)],
        "profile": [surfaces.Form("Create token", surfaces.MODAL)],
    }
    for view, button in (("users", "Create user"), ("sso", "Add provider"), ("profile", "Create token")):
        page = {"users": "admin_users.jsx", "sso": "sso_admin.jsx", "profile": "api_tokens.jsx"}[view]
        assert button in (ROOT / "ui" / "components" / page).read_text(encoding="utf-8"), (view, button)


def test_a_form_is_never_rooted_at_its_page_so_the_sweep_cannot_record_the_page_as_the_form() -> None:
    """B1': the provider entries named the overlay as their root, which was already on screen when the button was pressed, so the sweep recorded an open menu and never the form."""
    for table in (surfaces.LEGACY_FORMS, surfaces.PLATFORM_FORMS, surfaces.SYSTEM_FORMS):
        for page, forms in table.items():
            for form in forms:
                assert form.button.strip() and form.root.strip(), (page, form)
                assert form.root not in surfaces.PAGE_ROOTS, f"{page} / {form.button}: the form's root is the page's own root, present before the click"
                assert form.root in surfaces.FORM_ROOTS, (page, form.root)


def test_a_provider_form_opens_through_the_first_kind_of_the_register_menu() -> None:
    """The 'Register provider' button is a dropdown toggle (PC_RegisterDropdown, PC_RegisterAll): the form is a Modal that opens after a menu item is picked."""
    assert 'data-testid={`provider-register-kind-${k}`}' in CATALOG_JSX and 'data-testid={`provider-register-type-${cls.key}`}' in CATALOG_JSX
    registers = [(route, form) for route, forms in surfaces.LEGACY_FORMS.items() for form in forms if form.button == "Register provider"]
    registers += [(nav, form) for nav, forms in surfaces.PLATFORM_FORMS.items() for form in forms if form.button == "Register provider"]
    assert len(registers) == 9, [route for route, _ in registers]   # seven crud classes, the semantic-search stores and the Platform page's all-types menu (artifact storage: next test)
    for route, form in registers:
        assert form.root == surfaces.MODAL, (route, form)
        assert form.then in ('[data-testid^="provider-register-kind-"]', '[data-testid^="provider-register-type-"]'), (route, form)
    for table in (surfaces.LEGACY_FORMS, surfaces.PLATFORM_FORMS, surfaces.SYSTEM_FORMS):
        for page, forms in table.items():
            for form in forms:
                assert form.then is None or form.button == "Register provider", (page, form)


def test_a_register_menu_that_is_a_dead_end_is_asserted_dead_not_just_excused() -> None:
    """The artifact storage class serves no ``/_types``, so its menu says "No kinds available." and no form can be opened (ticket 01a1214c). The sweep checks the menu is still dead."""
    assert surfaces.DEAD_REGISTER_MENUS == {"providers/artifact_storage": "Register provider"}
    assert surfaces.DEAD_MENU_TEXT in CATALOG_JSX, "the text the console draws for a menu without kinds"
    for route in surfaces.DEAD_REGISTER_MENUS:
        assert surfaces.LEGACY_FORMS[route] == [], route
        assert "ticket" in surfaces.NO_FORM[f"overlay-page {route}"], "an exemption names the ticket that removes it"


def test_every_platform_create_button_the_console_draws_has_a_form_entry() -> None:
    """N4: the buttons come from NV_PLAT_PAGES (``createLabel`` and ``extraNav``), not from this table's memory of them."""
    pages = _plat_pages()
    assert set(pages) | {"providers"} == set(surfaces.PLATFORM_FORMS), "NV_PLAT_PAGES and the table list different pages"
    for nav, source in pages.items():
        buttons = [form.button for form in surfaces.PLATFORM_FORMS[nav]]
        create = re.search(r'createLabel: "([^"]+)"', source)
        if create:
            assert create.group(1) in buttons, f"{nav}: the console draws {create.group(1)!r}"
        extra = re.search(r'extraNav: \{\s*label: "([^"]+)",\s*run: function \(con(?:, setModal)?\) \{ ?([^}]*)', source)
        if extra:
            opens_form = "setModal(" in extra.group(2)
            assert (extra.group(1) in buttons) == opens_form, f"{nav}: {extra.group(1)!r} {'opens a form' if opens_form else 'opens an overlay'}"
        if not create and not extra:
            assert not buttons, f"{nav}: the console draws no create button"
            assert f"platform-view {nav}" in surfaces.NO_FORM
    assert [form.button for form in surfaces.PLATFORM_FORMS["providers"]] == ["Register provider"]


def test_the_legacy_routes_are_the_ones_the_shell_helper_opens() -> None:
    for route in surfaces.LEGACY_FORMS:
        assert overlay_target(route), route


def test_the_overlay_grammar_is_swept_section_by_section() -> None:
    """N5: health, the semantic-search provider (its own create modal) and the model profiles are overlay sections too; every provider class is a route."""
    classes = re.findall(r'\{ key: "(\w+)", label: ', CATALOG_JSX[CATALOG_JSX.index("const PROVIDER_CLASSES"):CATALOG_JSX.index("const PC_ALL_TYPE_CHIPS")])
    assert len(classes) == 12, classes
    swept = {overlay_target(route) for route in surfaces.LEGACY_FORMS}
    for key in classes:
        assert f"providers:{key}" in swept, f"the provider class {key!r} is not swept"
    assert {"workers:health", "providers:ssp", "providers:model_profile"} <= swept


def test_every_legacy_page_names_the_title_the_console_gives_its_overlay() -> None:
    """The overlay's title is section specific ("Channel rules", "Health"): it is what tells 'channels:rules' from 'channels' when both sit under the same overlay testid."""
    titles, sections = _titles("NV_OVERLAY_TITLES"), _titles("NV_OVERLAY_SECTION_TITLES")
    assert sorted(surfaces.LEGACY_TITLES) == sorted(surfaces.LEGACY_FORMS)
    for route, title in surfaces.LEGACY_TITLES.items():
        name, _, rest = overlay_target(route).partition(":")
        section = rest.split(":")[0]
        assert title == sections.get(f"{name}:{section}") or (title == titles[name] and f"{name}:{section}" not in sections), (route, title)


def test_every_provider_route_waits_for_the_body_of_its_own_class() -> None:
    assert 'data-testid={`provider-body-${classKey}`}' in CATALOG_JSX
    for route in surfaces.LEGACY_FORMS:
        name, _, rest = overlay_target(route).partition(":")
        markers = surfaces.ready_selectors("overlay-page", route)
        if name == "providers":
            assert f'[data-testid="provider-body-{rest.split(":")[0]}"]' in markers, route
        assert f'[data-testid="nv-overlay-title"]:text-is("{surfaces.LEGACY_TITLES[route]}")' in markers, route


def test_a_platform_view_waits_for_its_own_page_and_for_its_list_to_load() -> None:
    assert '"nv-plat-page:" + nav' in PLATFORM_JSX and 'data-testid="nv-plat-page:providers"' in PLATFORM_JSX
    assert 'data-testid="nv-plat-empty"' in PLATFORM_JSX and 'data-testid={"nv-pcard:" + c.name}' in PLATFORM_JSX
    for nav in surfaces.PLATFORM_FORMS:
        markers = surfaces.ready_selectors("platform-view", nav)
        assert markers[0] == f'[data-testid="nv-plat-page:{nav}"]', nav
        assert len(markers) == 2, f"{nav}: the page, and the proof that its list or body has loaded"
    assert surfaces.ready_selectors("platform-view", "providers")[1] == '[data-testid="provider-body-all"]'
    assert surfaces.ready_selectors("platform-view", "agents")[1] == '[data-testid="nv-plat-empty"], [data-testid^="nv-pcard:"]'


def test_a_system_view_waits_for_its_own_page_not_the_first_nav_row_it_falls_back_to() -> None:
    assert 'data-testid={"nv-sys-page:" + nav}' in SYSTEM_JSX and ": navs[0];" in SYSTEM_JSX, "NV_System falls back to the first nav row"
    for nav in surfaces.SYSTEM_VIEWS:
        assert surfaces.ready_selectors("system-view", nav) == [f'[data-testid="nv-sys-page:{nav}"]']


def test_a_phone_tab_waits_for_its_own_panel() -> None:
    for tab in surfaces.PHONE_TABS:
        assert f'data-testid="nv-mobile-panel:{tab.lower()}"' in MOBILE_JSX, tab
        assert surfaces.ready_selectors("phone", tab) == [f'[data-testid="nv-mobile-panel:{tab.lower()}"]']


def test_the_expected_visit_list_has_no_duplicates_and_covers_each_kind_of_surface() -> None:
    names = surfaces.expected_surfaces(7)
    assert len(names) == len(set(names)), "a surface is recorded under one name"
    kinds = {n.split(" ")[0] for n in names}
    assert {"studio", "session", "overlay", "overlay-page", "platform-view", "system-view", "graph", "phone"} <= kinds
    assert sum(n.startswith("graph builder / step ") for n in names) == 7
    assert names[:2] == ["studio", "session document"]


def test_every_form_is_in_the_visit_list_under_its_button() -> None:
    names = set(surfaces.expected_surfaces(1))
    for kind, table in (("overlay-page", surfaces.LEGACY_FORMS), ("platform-view", surfaces.PLATFORM_FORMS), ("system-view", surfaces.SYSTEM_FORMS)):
        for page, forms in table.items():
            for form in forms:
                assert surfaces.form_surface(kind, page, form.button) in names


def test_every_surface_has_a_floor_on_its_body_and_none_that_is_not_visited() -> None:
    """B3': the floor counts the controls of the BODY (the chrome of the overlay, the modal, the phone and the builder is not counted), per surface."""
    assert sorted(surfaces.FLOORS) == sorted(surfaces.expected_surfaces(7))
    for name, floor in surfaces.FLOORS.items():
        assert isinstance(floor, int) and floor >= 0, (name, floor)
    for modal in (n for n in surfaces.FLOORS if " / " in n and n.split(" ")[0] in {"overlay-page", "platform-view", "system-view"}):
        assert surfaces.FLOORS[modal] >= 1, f"{modal}: a form with no control in its body is the form that failed to draw"
    for bar in ('.modal-h', '.modal-f', '.nv-overlay-head', '.nv-plat-head', '[role="tablist"]', '[data-testid="gb-topbar"]'):
        assert bar in surfaces.CHROME, bar


def test_the_phone_has_its_four_tabs() -> None:
    assert surfaces.PHONE_TABS == ["Inbox", "Spaces", "Files", "More"]
