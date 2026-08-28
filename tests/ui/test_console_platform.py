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


def test_nav_covers_the_grammar():
    # Every nav id the URL grammar admits renders a row; a grammar id
    # with no page would be an address that renders nothing.
    for nav in _grammar_platform_navs():
        if nav == "providers":
            continue  # providers is the special-cased family page
        assert f"{nav}: {{" in PLAT, f"no page config for {nav}"
    for group in ("Intelligence", "Workbench", "Automation", "Governance"):
        assert group in PLAT


def test_cards_open_shared_overlays():
    # Cards address overlays (the shared-surface contract), never
    # private routes: every open/create goes through con.openOverlay.
    opens = re.findall(r"con\.openOverlay\(\"([a-z-]+)\"", PLAT)
    assert set(opens) >= {"agents", "graphs", "toolsets", "collections",
                          "workspaces", "new-workspace", "triggers",
                          "channels", "harnesses", "services",
                          "approvals", "providers"}


def test_delete_is_confirm_guarded():
    m = re.search(r"function del\(row\)[\s\S]{0,700}", PLAT)
    assert m and "confirmDialog" in m.group(0)
    assert "Referenced entities refuse deletion" in PLAT


def test_search_query_resets_the_page():
    # R4 review finding 3: without this, Math.min(pageNo, pages - 1)
    # clamps toward the END of a search-narrowed range, so typing a
    # query while on a later page could land on the LAST page of
    # matches instead of the first. Mirrors toolsets.jsx:1259's
    # equivalent pattern for its own text/policy filters.
    m = re.search(r"React\.useEffect\(function \(\) \{ setPageNo\(0\); \}, \[q\]\);", PLAT)
    assert m, "pageNo must reset when the search query q changes"


def test_providers_ride_the_real_plurals():
    for plural in ("llm_providers", "embedding_providers", "tts_providers",
                   "web_search_providers", "workspace_providers",
                   "channel_providers", "ssp"):
        assert plural in PLAT, plural


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
    for cls in (".nv-plat-nav", ".nv-pcard-grid", ".nv-fam-pill",
                ".nv-audit-table", ".nv-plat-empty"):
        assert cls in CSS, cls
