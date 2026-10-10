"""What the standing a11y sweep visits, as data (console review C-003): every page it opens, how each create form opens, how each surface proves it is THE surface, and how much of it must be drawn.

Kept in a module of its own, with no browser in it, so that a unit test (``tests/ui/test_a11y_sweep_surfaces.py``) can hold the tables to the console's own: a Platform or System view added to the
shell without a row here fails that test, and a row cannot be dropped without it noticing. ``expected_surfaces`` is the exact list of surface names a complete sweep records, in order; the sweep
asserts it visited exactly that, so a form that silently failed to open (the old helper returned quietly when its button was missing) is a failure, not a pass.

A form entry is ``Form(button, root, then)``: the button that is pressed, the root of what it opens, and (only for the provider register menus) the menu item that is picked after it. ``MODAL`` is
the dialog nearly every form opens; the Platform page's New workspace opens the New workspace overlay. A form root is never the root of its page (the page is on screen before the button is pressed,
so a sweep rooted there records the page and calls it the form); the sweep also asserts the root is absent before the click. An empty list says the page has no form, and ``NO_FORM`` says why.

Only the FIRST kind's form of a provider class is swept (the first item of its register menu): the other kinds draw other fields and are not looked at.

``ready_selectors`` is what a surface must show before it is looked at: its own page (not a neighbour's, not the first row of the nav that a view falls back to) and the proof that its list or body
has loaded. ``FLOORS`` is the fewest controls the BODY of a surface may hold (``CHROME`` is the fixed furniture around it that does not count): about half of what each held when the floors were
set, on an install with nothing but the sweep's own seeds; ``counts_table`` prints the numbers on every run.
"""

from __future__ import annotations

from typing import NamedTuple

MODAL = ".modal"
OVERLAY = '[data-testid^="nv-overlay:"]'
PLATFORM = '[data-testid="nv-platform"]'
SYSTEM = '[data-testid="nv-view:system"]'
NEW_WORKSPACE_OVERLAY = '[data-testid="nv-overlay:new-workspace"]'

# the provider classes whose page is a panel of its own (ProviderCatalog's ``form: "panel"``): they have no card grid and no ``provider-empty-<key>``
PANEL_CLASSES = {"model_profile", "ssp", "workspace", "channel"}

PAGE_ROOTS = {OVERLAY, PLATFORM, SYSTEM}
FORM_ROOTS = {MODAL, NEW_WORKSPACE_OVERLAY}

KIND_MENU_ITEM = '[data-testid^="provider-register-kind-"]'    # PC_RegisterDropdown: a menu of the kinds of one class
TYPE_MENU_ITEM = '[data-testid^="provider-register-type-"]'    # PC_RegisterAll: a menu of the classes, on the Platform page that shows them all

# the fixed furniture of a surface, whose controls are examined for names and not counted toward its floor: a modal's title and footer, an overlay's head and foot, the Platform page's header, the
# phone's tab bar, the graph builder's top bar
CHROME = '.modal-h, .modal-f, .sheet-h, .sheet-f, .nv-overlay-head, .nv-overlay-foot, .nv-plat-head, [role="tablist"], [data-testid="gb-topbar"]'


class Form(NamedTuple):
    button: str
    root: str
    then: str | None = None


def _register(menu_item: str) -> Form:
    """A provider form opens in two steps: 'Register provider' is a dropdown toggle, and the form (a Modal) opens when a kind of the menu is picked."""
    return Form("Register provider", MODAL, menu_item)


# the Platform pages that are overlays, opened by their legacy route
LEGACY_FORMS: dict[str, list[Form]] = {
    "agents": [Form("New agent", MODAL)],
    "graphs": [Form("New graph", MODAL)],
    "triggers": [Form("Create trigger", MODAL)],
    "toolsets": [Form("New toolset", MODAL)],
    "approvals": [],
    "workers": [],
    "health": [],
    "harnesses": [Form("Register harness", MODAL)],
    "services": [Form("New service", MODAL)],
    "workspaces": [Form("New workspace", MODAL)],
    "workspaces/templates": [Form("New template", MODAL)],
    "workspaces/providers": [Form("New provider", MODAL)],
    "channels": [Form("New channel", MODAL)],          # enabled only while a channel provider exists: the sweep seeds one
    "channels/rules": [Form("New rule", MODAL)],        # its toolbar draws only while a channel provider exists
    "channels/providers": [Form("New provider", MODAL)],
    "knowledge/collections": [Form("New collection", MODAL)],
    "subsystems/internal-collections": [Form("Configure", MODAL)],
    "providers/llm": [_register(KIND_MENU_ITEM)],
    "providers/embedding": [_register(KIND_MENU_ITEM)],
    "providers/cross_encoder": [_register(KIND_MENU_ITEM)],
    "providers/stt": [_register(KIND_MENU_ITEM)],
    "providers/tts": [_register(KIND_MENU_ITEM)],
    "providers/web_search": [_register(KIND_MENU_ITEM)],
    "providers/web_fetch": [_register(KIND_MENU_ITEM)],
    "providers/artifact_storage": [_register(KIND_MENU_ITEM)],   # db, filesystem and s3 since the class serves its kinds (GET /_types); the first, db, is the one that is built
    "providers/model_profile": [Form("New profile", MODAL)],
    "ssp": [_register(KIND_MENU_ITEM)],                 # the semantic-search stores: the same register menu, and a modal of their own
}

# the title each of those overlays shows (nv-overlay-title): it is section specific, which is what tells "channels:rules" from "channels" under one overlay testid
LEGACY_TITLES: dict[str, str] = {
    "agents": "Agents",
    "graphs": "Graphs",
    "triggers": "Triggers",
    "toolsets": "Toolsets",
    "approvals": "Approvals",
    "workers": "Workers",
    "health": "Health",
    "harnesses": "Harnesses",
    "services": "Services",
    "workspaces": "Workspaces",
    "workspaces/templates": "Workspace templates",
    "workspaces/providers": "Providers",
    "channels": "Channels",
    "channels/rules": "Channel rules",
    "channels/providers": "Providers",
    "knowledge/collections": "Collections",
    "subsystems/internal-collections": "Internal collections",
    "providers/llm": "Providers",
    "providers/embedding": "Providers",
    "providers/cross_encoder": "Providers",
    "providers/stt": "Providers",
    "providers/tts": "Providers",
    "providers/web_search": "Providers",
    "providers/web_fetch": "Providers",
    "providers/artifact_storage": "Providers",
    "providers/model_profile": "Providers",
    "ssp": "Providers",
}

# the Platform VIEW (view=platform:<id>: what every Platform nav row opens), one entry per id of the shell's grammar (ui/foundation/shell-url.js SH_VIEWS.platform)
PLATFORM_FORMS: dict[str, list[Form]] = {
    "providers": [_register(TYPE_MENU_ITEM)],
    "profiles": [Form("New profile", MODAL)],
    "toolsets": [Form("New toolset", MODAL)],
    "tools": [],
    "collections": [Form("New collection", MODAL)],
    "workspaces": [Form("New workspace", NEW_WORKSPACE_OVERLAY)],
    "templates": [Form("New template", MODAL)],
    "agents": [Form("New agent", MODAL)],
    "graphs": [Form("New graph", MODAL)],
    "triggers": [Form("New trigger", MODAL)],
    "channels": [Form("New channel", MODAL)],
    "harnesses": [Form("New harness", MODAL), Form("Build outbound", MODAL)],
    "services": [Form("New service", MODAL)],
    "approvals": [Form("New policy", MODAL)],
}

# the System VIEWS (view=system:<id>), one entry per id of ui/foundation/shell-url.js SH_VIEWS.system, and the create forms three of them open from their own page
SYSTEM_VIEWS: list[str] = ["dashboard", "users", "apikeys", "sso", "mcp", "internal", "activity", "setup", "profile"]

SYSTEM_FORMS: dict[str, list[Form]] = {
    "users": [Form("Create user", MODAL)],
    "sso": [Form("Add provider", MODAL)],
    "profile": [Form("Create token", MODAL)],           # the Profile page embeds the personal token page
}

NO_FORM: dict[str, str] = {
    "overlay-page approvals": "its policies are created on the Platform view (platform-view approvals, New policy)",
    "overlay-page workers": "a list of workers with Drain; nothing is created from it",
    "overlay-page health": "the health of the workers, read only; nothing is created from it",
    "platform-view tools": "the tool catalogue is read-only",
    "system-view dashboard": "the dashboard shows the install's state; nothing is created from it",
    "system-view apikeys": "the admin view of every user's tokens only lists and revokes; a token is created from Profile",
    "system-view mcp": "the MCP page lists the install's MCP servers and their tools; nothing is created from it",
    "system-view internal": "its Configure form is swept through the overlay-page of the internal collections",
    "system-view activity": "the activity feed is read only",
    "system-view setup": "the readiness checks and their fix actions; its provider fix action opens the setup wizard, which creates a provider and is not swept",
}

PHONE_TABS = ["Inbox", "Spaces", "Files", "More"]
OVERLAYS = ["new-session", "new-workspace", "activity"]


def form_surface(kind: str, page: str, button: str) -> str:
    return f"{kind} {page} / {button}"


def overlay_title_selector(title: str) -> str:
    return f'[data-testid="nv-overlay-title"]:text-is("{title}")'


def ready_selectors(kind: str, name: str) -> list[str]:
    """What must be visible before a surface is looked at, each a CSS selector: its own page, and the proof that it has loaded.

    ``platform-view`` / ``system-view`` / ``overlay-page`` take the page's id or route; ``phone`` a tab's label."""
    if kind == "platform-view":
        page = f'[data-testid="nv-plat-page:{name}"]'
        if name == "providers":
            return [page, '[data-testid="provider-body-all"]', _providers_loaded("all")]
        return [page, '[data-testid="nv-plat-empty"], [data-testid^="nv-pcard:"]']
    if kind == "system-view":
        return [f'[data-testid="nv-sys-page:{name}"]']
    if kind == "overlay-page":
        markers = [overlay_title_selector(LEGACY_TITLES[name])]
        if LEGACY_TITLES[name] == "Providers":
            key = _provider_class(name)
            markers.append(f'[data-testid="provider-body-{key}"]')
            if key not in PANEL_CLASSES:
                markers.append(_providers_loaded(key))
        return markers
    if kind == "phone":
        return [f'[data-testid="nv-mobile-panel:{name.lower()}"]']
    raise KeyError(kind)


def _providers_loaded(key: str) -> str:
    """A provider class has loaded its instances when it shows a card or says it has none; ``provider-body-<key>`` is drawn at once, with "No providers match" in it while the list loads."""
    return f'[data-testid="provider-empty-{key}"], [data-testid="provider-body-{key}"] .pc-card'


def _provider_class(route: str) -> str:
    """The provider class an overlay route shows: ``providers/llm`` -> ``llm``, ``workspaces/providers`` -> ``workspace``, ``channels/providers`` -> ``channel``, ``ssp`` -> ``ssp``."""
    if route == "ssp":
        return "ssp"
    if route.endswith("/providers"):
        return route.split("/")[0].rstrip("s")
    return route.split("/")[1]


def expected_surfaces(builder_steps: int) -> list[str]:
    """Every surface name a complete sweep records, in order. ``builder_steps`` is the number of steps of the seeded graph (one outline row each)."""
    names = ["studio", "session document"]
    names += [f"overlay {name}" for name in OVERLAYS]
    for route, forms in LEGACY_FORMS.items():
        names.append(f"overlay-page {route}")
        names += [form_surface("overlay-page", route, form.button) for form in forms]
    for nav, forms in PLATFORM_FORMS.items():
        names.append(f"platform-view {nav}")
        names += [form_surface("platform-view", nav, form.button) for form in forms]
    for nav in SYSTEM_VIEWS:
        names.append(f"system-view {nav}")
        names += [form_surface("system-view", nav, form.button) for form in SYSTEM_FORMS.get(nav, [])]
    names += ["graph builder"] + [f"graph builder / step {i + 1}" for i in range(builder_steps)] + ["graph builder / JSON import", "graph builder / palette"]
    names += [f"phone / {tab}" for tab in PHONE_TABS]
    return names


# The fewest controls in the BODY of each surface (see the module docstring). Set from the counts table of a run on a fresh install; a surface that examines fewer is a surface that did not draw.
FLOORS: dict[str, int] = {
    "studio": 8,
    "session document": 12,
    "overlay new-session": 2,
    "overlay new-workspace": 1,
    "overlay activity": 4,
    "overlay-page agents": 2,
    "overlay-page agents / New agent": 14,
    "overlay-page graphs": 2,
    "overlay-page graphs / New graph": 2,
    "overlay-page triggers": 1,
    "overlay-page triggers / Create trigger": 1,
    "overlay-page toolsets": 2,
    "overlay-page toolsets / New toolset": 2,
    "overlay-page approvals": 0,                        # a list page whose body, on a fresh install, is its empty state with no control in it (the policies are created on the Platform view)
    "overlay-page workers": 1,
    "overlay-page health": 1,
    "overlay-page harnesses": 3,
    "overlay-page harnesses / Register harness": 3,
    "overlay-page services": 1,
    "overlay-page services / New service": 1,
    "overlay-page workspaces": 6,
    "overlay-page workspaces / New workspace": 2,
    "overlay-page workspaces/templates": 1,
    "overlay-page workspaces/templates / New template": 4,
    "overlay-page workspaces/providers": 2,
    "overlay-page workspaces/providers / New provider": 1,
    "overlay-page channels": 1,
    "overlay-page channels / New channel": 2,
    "overlay-page channels/rules": 2,
    "overlay-page channels/rules / New rule": 6,
    "overlay-page channels/providers": 3,
    "overlay-page channels/providers / New provider": 2,
    "overlay-page knowledge/collections": 1,
    "overlay-page knowledge/collections / New collection": 2,
    "overlay-page subsystems/internal-collections": 1,
    "overlay-page subsystems/internal-collections / Configure": 2,
    "overlay-page providers/llm": 4,
    "overlay-page providers/llm / Register provider": 5,
    "overlay-page providers/embedding": 1,
    "overlay-page providers/embedding / Register provider": 5,
    "overlay-page providers/cross_encoder": 1,
    "overlay-page providers/cross_encoder / Register provider": 4,
    "overlay-page providers/stt": 3,
    "overlay-page providers/stt / Register provider": 5,
    "overlay-page providers/tts": 3,
    "overlay-page providers/tts / Register provider": 5,
    "overlay-page providers/web_search": 4,
    "overlay-page providers/web_search / Register provider": 2,
    "overlay-page providers/web_fetch": 3,
    "overlay-page providers/web_fetch / Register provider": 2,
    "overlay-page providers/artifact_storage": 3,
    "overlay-page providers/artifact_storage / Register provider": 2,
    "overlay-page providers/model_profile": 3,
    "overlay-page providers/model_profile / New profile": 2,
    "overlay-page ssp": 1,
    "overlay-page ssp / Register provider": 4,
    "platform-view providers": 6,
    "platform-view providers / Register provider": 5,
    "platform-view profiles": 1,
    "platform-view profiles / New profile": 2,
    "platform-view toolsets": 1,
    "platform-view toolsets / New toolset": 2,
    "platform-view tools": 1,
    "platform-view collections": 0,                     # likewise: the empty state of an install with no collection has no control; the page and its list having loaded is the check
    "platform-view collections / New collection": 2,
    "platform-view workspaces": 1,
    "platform-view workspaces / New workspace": 1,
    "platform-view templates": 1,
    "platform-view templates / New template": 2,
    "platform-view agents": 3,
    "platform-view agents / New agent": 14,
    "platform-view graphs": 1,
    "platform-view graphs / New graph": 2,
    "platform-view triggers": 1,
    "platform-view triggers / New trigger": 1,
    "platform-view channels": 1,
    "platform-view channels / New channel": 2,
    "platform-view harnesses": 1,
    "platform-view harnesses / New harness": 3,
    "platform-view harnesses / Build outbound": 3,
    "platform-view services": 1,
    "platform-view services / New service": 1,
    "platform-view approvals": 5,
    "platform-view approvals / New policy": 8,
    "system-view dashboard": 1,
    "system-view users": 1,
    "system-view users / Create user": 3,
    "system-view apikeys": 1,
    "system-view sso": 2,
    "system-view sso / Add provider": 3,
    "system-view mcp": 10,
    "system-view internal": 1,
    "system-view activity": 4,
    "system-view setup": 2,
    "system-view profile": 3,
    "system-view profile / Create token": 1,
    "graph builder": 3,
    "graph builder / step 1": 5,
    "graph builder / step 2": 13,
    "graph builder / step 3": 5,
    "graph builder / step 4": 5,
    "graph builder / step 5": 10,
    "graph builder / step 6": 4,
    "graph builder / step 7": 5,
    "graph builder / JSON import": 1,
    "graph builder / palette": 5,
    "phone / Inbox": 1,
    "phone / Spaces": 2,
    "phone / Files": 1,
    "phone / More": 8,
}
