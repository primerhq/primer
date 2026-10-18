"""The Platform view (wiring plan P4 T10).

Static pins over nv-platform.jsx: the nav covers every PLATNAV id the
URL grammar admits, the card pages open the SHARED overlays rather than
private routes, delete is confirm-guarded, and providers ride the real
per-class plurals.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
PLAT = (UI / "components" / "console" / "nv-platform.jsx").read_text(
    encoding="utf-8")
URLJS = (UI / "foundation" / "shell-url.js").read_text(encoding="utf-8")
HTML = (UI / "index.html").read_text(encoding="utf-8")
CSS = (UI / "styles.css").read_text(encoding="utf-8")


def _grammar_platform_navs() -> list[str]:
    m = re.search(r"platform: \[([\s\S]*?)\]", URLJS)
    assert m
    return re.findall(r'"([a-z]+)"', m.group(1))


def _custom_pages() -> dict[str, str]:
    """``NV_PLAT_CUSTOM_PAGES``: the nav ids whose page is a component exported on ``window`` by another file (seam I-2), read from the source."""
    m = re.search(r"var NV_PLAT_CUSTOM_PAGES = \{([^}]*)\}", PLAT)
    assert m, "NV_PLAT_CUSTOM_PAGES is gone"
    return dict(re.findall(r'(\w+): "(\w+)"', m.group(1)))


def _sidebar_groups() -> list[tuple[str, list[str]]]:
    """``NV_PLAT_GROUPS`` from the source: (label, ids) per group."""
    block = PLAT[PLAT.index("var NV_PLAT_GROUPS = ["):]
    block = block[:block.index("\n];")]
    return [(label, re.findall(r'"(\w+)"', ids)) for label, ids in re.findall(r'label: "(\w+)",\s*ids: \[([^\]]*)\]', block)]


def test_nav_covers_the_grammar():
    # Every nav id the URL grammar admits renders a row; a grammar id
    # with no page would be an address that renders nothing.
    custom = _custom_pages()
    for nav in _grammar_platform_navs():
        if nav == "providers":
            continue  # providers mounts window.ProviderCatalog inline, not an NV_PLAT_PAGES entry
        if nav in custom:
            # a custom page (seam I-2, Platform revamp Plan 01 Task 7): its component is exported on window by its own file and its row exists while it is loaded
            assert f"{nav}: {{" not in PLAT.split("var NV_PLAT_PAGES = {", 1)[1].split("\n};", 1)[0], f"{nav} is a custom page: it has no generic page config"
            continue
        assert f"{nav}: {{" in PLAT, f"no page config for {nav}"
    assert set(custom) <= set(_grammar_platform_navs()), "a custom page for an id the grammar does not admit is an address nobody can reach"
    for group in ("Intelligence", "Workbench", "Automation", "Governance"):
        assert group in PLAT


def test_the_sidebar_order_is_the_grammar_order():
    """Platform revamp Plan 01 Task 7 (design 4.1): ``SH_VIEWS.platform`` is ``NV_PLAT_GROUPS`` flattened, so the order of the sidebar and the order of the grammar cannot drift."""
    groups = _sidebar_groups()
    assert [label for label, _ in groups] == ["Intelligence", "Workbench", "Automation", "Governance"]
    assert [nav for _, ids in groups for nav in ids] == _grammar_platform_navs()
    assert "workspaces" not in [nav for _, ids in groups for nav in ids], "Workspaces left the Platform view (design 14.1)"


def test_cards_open_shared_overlays():
    # Cards address overlays (the shared-surface contract), never
    # private routes: every open/create goes through con.openOverlay.
    #
    # RETARGET (IA restructure 01a04d6a): "providers" dropped out of this
    # set on purpose - the user directive kills the platform-page ->
    # catalog-overlay -> form-overlay stack, so NV_PlatPage now mounts
    # window.ProviderCatalog directly (see NV_ProvidersPlatPage) instead
    # of addressing it through con.openOverlay at all.
    opens = re.findall(r"con\.openOverlay\(\"([a-z-]+)\"", PLAT)
    #
    # RETARGET (Platform revamp Plan 01 Task 7, design 14.1): "workspaces" and
    # "new-workspace" dropped out on purpose - the Workspaces page left the
    # Platform view, so no card of this file opens the workspace overlay (the
    # studio rail's workspace menu still does: nv-chrome.jsx).
    assert set(opens) >= {"agents", "graphs", "toolsets", "collections",
                          "triggers", "channels", "harnesses", "services",
                          "approvals"}
    assert "providers" not in opens
    assert "workspaces" not in opens and "new-workspace" not in opens


def test_delete_is_confirm_guarded():
    # The confirm-then-DELETE flow lives in NV_deleteRow (run for real by
    # tests/ui/test_platform_delete_confirm.py); the card's del() hands it
    # the console's dialog. The prompt must come before the DELETE.
    m = re.search(r"function NV_deleteRow\([\s\S]{0,900}", PLAT)
    assert m, "NV_deleteRow is gone"
    flow = m.group(0)
    assert "env.confirmDialog(" in flow
    assert flow.index("env.confirmDialog(") < flow.index('env.apiFetch("DELETE"')
    assert re.search(r"function del\(row\)[\s\S]{0,700}?NV_deleteRow\(", PLAT)
    assert "Referenced entities refuse deletion" in PLAT


def test_search_query_resets_the_page():
    # R4 review finding 3: without a reset, a clamp toward the END of a
    # search-narrowed range meant typing a query while on a later page
    # could land on the LAST page of matches instead of the first. The
    # page now pages through the kit's PK_usePagedFilter (Platform revamp
    # Plan 01 Task 10, design 5.1), whose setQ sets the query AND page 0
    # in one call (tests/ui/test_platform_kit_page.py runs that); this pin
    # keeps the page on it, with no page number of its own to forget.
    assert "PK_usePagedFilter(cards, { pageSize: NV_PLAT_PAGE_SIZE, text: NV_platFilterText })" in PLAT
    assert "onChange: paged.setQ" in PLAT, "the filter box reports through the kit's setQ"
    assert "setPageNo" not in PLAT and "pageNo" not in PLAT, "the page keeps no page number of its own"


def test_providers_ride_the_real_plurals():
    # RETARGET (IA restructure 01a04d6a): nv-platform.jsx no longer
    # carries its own module-local copy of the provider-class registry
    # (NV_PROV_CLASSES, deleted) - it mounts window.ProviderCatalog
    # inline instead, which owns the one real PROVIDER_CLASSES registry.
    # The check moves with the data.
    catalog = (UI / "components" / "provider-catalog.jsx").read_text(encoding="utf-8")
    for plural in ("llm_providers", "embedding_providers", "tts_providers",
                   "web_search_providers", "workspace_providers",
                   "channel_providers", "ssp"):
        assert plural in catalog, plural
    assert "NV_PROV_CLASSES" not in PLAT


def test_approvals_page_carries_the_audit():
    assert "NV_ApprovalsAudit" in PLAT
    assert "approvalRecords" in PLAT
    assert "decided_by" in PLAT, "P6's approver routing lands here"


def test_manifest_views_match_the_grammar():
    man = json.loads((UI / "fixtures" / "shell" / "manifest.json").read_text(
        encoding="utf-8"))
    assert man["views"]["platform"] == _grammar_platform_navs()


def test_script_and_css_landed():
    assert "components/console/nv-platform.jsx" in HTML
    # The generic pages draw through the kit (Platform revamp Plan 01 Task 10): its grid and card replace .nv-pcard-grid, and the family pills went with
    # the providers UI this page used to draw itself.
    for cls in (".nv-plat-nav", ".pk-grid", ".pk-card",
                ".nv-audit-table", ".nv-plat-empty"):
        assert cls in CSS, cls
