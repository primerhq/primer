"""Every overlay name resolves to a real component, and every component
named is one that actually exists on window today.

This is the concrete form of the section 9 risk: the palette-as-router
model must not orphan a rarely-used admin surface. An entry pointing at
a component nobody exports is an orphan that only shows up when a user
opens it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
HOST = UI / "components" / "shell" / "sh-overlay-host.jsx"
MANIFEST = UI / "fixtures" / "shell" / "manifest.json"

# name -> (file, exported component) from spec section 5's designations.
#
# providers: S4 P4 Task 34 deletes providers.jsx and forbids
# window.ProvidersPage anywhere under ui/; the successor is S4 Task 18's
# props-only window.ProviderCatalog in provider-catalog.jsx.
# collections: S2 P4 Task 21 rebuilds knowledge.jsx but PINS the exported
# name CollectionsPage and its window global across the rebuild for exactly
# this row, so it is not renamed to KnowledgePage. Re-grep knowledge.jsx for
# `window.CollectionsPage =` rather than trusting the old line number.
DESIGNATIONS = {
    "providers": ("provider-catalog.jsx", "ProviderCatalog"),
    "collections": ("knowledge.jsx", "CollectionsPage"),
    "agents": ("agents.jsx", "AgentsPage"),
    "graphs": ("graphs.jsx", "GraphsPage"),
    "triggers": ("triggers.jsx", "TR_TriggersPage"),
    "toolsets": ("toolsets.jsx", "ToolsetsPage"),
    "tools": ("toolsets.jsx", "ToolsPage"),
    "workers": ("workers.jsx", "WorkersPage"),
    "approvals": ("approvals.jsx", "ApprovalsPage"),
    "harnesses": ("harnesses.jsx", "HarnessesPage"),
    "services": ("services.jsx", "SV_ServicesPage"),
    "channels": ("channels.jsx", "ChannelsPage"),
    "workspaces": ("workspaces.jsx", "WorkspacesPage"),
}


def _host() -> str:
    return HOST.read_text(encoding="utf-8")


def _mount_names() -> set[str]:
    m = re.search(r"var SH_OVERLAY_MOUNTS = \{([\s\S]*?)\n\};", _host())
    assert m, "the mount table must be a literal map"
    return set(re.findall(r'^\s{2}"?([\w-]+)"?:', m.group(1), re.MULTILINE))


def test_every_overlay_name_has_a_mount() -> None:
    """Admin arrives in Task 20; everything else must be here now."""
    names = json.loads(MANIFEST.read_text(encoding="utf-8"))["overlays"]
    missing = set(names) - _mount_names() - {"admin"}
    assert not missing, f"orphaned overlays: {sorted(missing)}"


def test_every_designated_component_is_actually_exported() -> None:
    src = _host()
    for name, (filename, component) in DESIGNATIONS.items():
        assert "window." + component in src, f"{name} -> {component}"
        exporter = (UI / "components" / filename).read_text(encoding="utf-8")
        assert component in exporter, f"{component} missing from {filename}"


def test_workers_and_channels_carry_their_second_surface() -> None:
    """Pinned decision 3 collapses Health into workers:health, and the
    section 3 list pairs channel instances with channel rules."""
    src = _host()
    assert "window.HealthPage" in src and '"health"' in src
    assert "window.ChannelRulesPage" in src and '"rules"' in src


def test_switching_workspace_is_a_verb() -> None:
    """Spec section 3: switching is a palette verb and a rail affordance."""
    src = (UI / "components" / "shell" / "sh-doc-host.jsx").read_text(
        encoding="utf-8"
    )
    assert "workspace.switch" in src
    assert "Switch Workspace" in src
