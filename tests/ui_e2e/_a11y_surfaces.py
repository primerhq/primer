"""What the standing a11y sweep visits, as data (console review C-003): every page it opens and, for each, how its create form opens.

Kept in a module of its own, with no browser in it, so that a unit test (``tests/ui/test_a11y_sweep_surfaces.py``) can hold the tables to the console's own lists: a Platform or System view
added to the shell without a row here fails that test, and a row cannot be dropped without it noticing. ``expected_surfaces`` is the exact list of surface names a complete sweep records, in
order; the sweep asserts it visited exactly that, so a form that silently failed to open (the old helper returned quietly when its button was missing) is a failure, not a pass.

A form entry is ``(button name, root of what it opens)``. ``MODAL`` is the dialog most forms open; a provider form opens INLINE in the overlay it was started from, and the Platform page's
New workspace opens the New workspace overlay. An empty list says the page has no form, and ``NO_FORM`` says why.
"""

from __future__ import annotations

MODAL = ".modal"
OVERLAY = '[data-testid^="nv-overlay:"]'
PLATFORM = '[data-testid="nv-platform"]'
SYSTEM = '[data-testid="nv-view:system"]'
NEW_WORKSPACE_OVERLAY = '[data-testid="nv-overlay:new-workspace"]'

Form = tuple[str, str]

# the Platform pages that are overlays, opened by their legacy route
LEGACY_FORMS: dict[str, list[Form]] = {
    "agents": [("New agent", MODAL)],
    "graphs": [("New graph", MODAL)],
    "triggers": [("Create trigger", MODAL)],
    "toolsets": [("New toolset", MODAL)],
    "approvals": [],
    "workers": [],
    "harnesses": [("Register harness", MODAL)],
    "services": [("New service", MODAL)],
    "workspaces": [("New workspace", MODAL)],
    "workspaces/templates": [("New template", MODAL)],
    "workspaces/providers": [("New provider", MODAL)],
    "channels": [("New channel", MODAL)],          # enabled only while a channel provider exists: the sweep seeds one
    "channels/rules": [("New rule", MODAL)],        # its toolbar draws only while a channel provider exists
    "channels/providers": [("New provider", MODAL)],
    "knowledge/collections": [("New collection", MODAL)],
    "subsystems/internal-collections": [("Configure", MODAL)],
    "providers/llm": [("Register provider", OVERLAY)],
    "providers/embedding": [("Register provider", OVERLAY)],
    "providers/cross_encoder": [("Register provider", OVERLAY)],
    "providers/stt": [("Register provider", OVERLAY)],
    "providers/tts": [("Register provider", OVERLAY)],
    "providers/web_search": [("Register provider", OVERLAY)],
    "providers/web_fetch": [("Register provider", OVERLAY)],
    "providers/artifact_storage": [("Register provider", OVERLAY)],
}

# the Platform VIEW (view=platform:<id>: what every Platform nav row opens), one entry per id of the shell's grammar (ui/foundation/shell-url.js SH_VIEWS.platform)
PLATFORM_FORMS: dict[str, list[Form]] = {
    "providers": [("Register provider", PLATFORM)],
    "profiles": [("New profile", MODAL)],
    "toolsets": [("New toolset", MODAL)],
    "tools": [],
    "collections": [("New collection", MODAL)],
    "workspaces": [("New workspace", NEW_WORKSPACE_OVERLAY)],
    "templates": [("New template", MODAL)],
    "agents": [("New agent", MODAL)],
    "graphs": [("New graph", MODAL)],
    "triggers": [("New trigger", MODAL)],
    "channels": [("New channel", MODAL)],
    "harnesses": [("New harness", MODAL), ("Build outbound", MODAL)],
    "services": [("New service", MODAL)],
    "approvals": [("New policy", MODAL)],
}

# the System VIEWS (view=system:<id>), one entry per id of ui/foundation/shell-url.js SH_VIEWS.system; none opens a create form from its nav
SYSTEM_VIEWS: list[str] = ["dashboard", "users", "apikeys", "sso", "mcp", "internal", "activity", "setup", "profile"]

NO_FORM: dict[str, str] = {
    "overlay-page approvals": "its policies are created on the Platform view (platform-view approvals, New policy)",
    "overlay-page workers": "a list of workers with Drain; nothing is created from it",
    "platform-view tools": "the tool catalogue is read-only",
}

PHONE_TABS = ["Inbox", "Spaces", "Files", "More"]
OVERLAYS = ["new-session", "new-workspace", "activity"]


def form_surface(kind: str, page: str, button: str) -> str:
    return f"{kind} {page} / {button}"


def expected_surfaces(builder_steps: int) -> list[str]:
    """Every surface name a complete sweep records, in order. ``builder_steps`` is the number of steps of the seeded graph (one outline row each)."""
    names = ["studio", "session document"]
    names += [f"overlay {name}" for name in OVERLAYS]
    for route, forms in LEGACY_FORMS.items():
        names.append(f"overlay-page {route}")
        names += [form_surface("overlay-page", route, button) for button, _root in forms]
    for nav, forms in PLATFORM_FORMS.items():
        names.append(f"platform-view {nav}")
        names += [form_surface("platform-view", nav, button) for button, _root in forms]
    names += [f"system-view {nav}" for nav in SYSTEM_VIEWS]
    names += ["graph builder"] + [f"graph builder / step {i + 1}" for i in range(builder_steps)] + ["graph builder / JSON import", "graph builder / palette"]
    names += [f"phone / {tab}" for tab in PHONE_TABS]
    return names
