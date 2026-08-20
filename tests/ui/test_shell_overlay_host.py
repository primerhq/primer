"""Overlays are re-hosted pages with their chrome removed.

Section 5 designates thirteen of the fourteen as reused components. The
danger of reuse is that a page carries its own navigation into a surface
that has no router, so the no-chrome contract is asserted statically
here, and pinned decision 14's shim is what keeps `useRouter` honest
after P5 deletes the real router.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
HOST = UI / "components" / "shell" / "sh-overlay-host.jsx"
SHIM = UI / "foundation" / "shell-router-shim.js"
MANIFEST = UI / "fixtures" / "shell" / "manifest.json"


def _host() -> str:
    return HOST.read_text(encoding="utf-8")


def _mount_names() -> set[str]:
    m = re.search(r"var SH_OVERLAY_MOUNTS = \{([\s\S]*?)\n\};", _host())
    assert m, "the mount table must be a literal map"
    return set(re.findall(r"^\s{2}(\w+):", m.group(1), re.MULTILINE))


def test_registered_in_the_bundle() -> None:
    index = (UI / "index.html").read_text(encoding="utf-8")
    assert 'src="components/shell/sh-overlay-host.jsx"' in index
    assert 'src="foundation/shell-router-shim.js"' in index
    assert "window.SH_OverlayHost = SH_OverlayHost;" in _host()


def test_the_catalog_lands_first_and_is_the_shipped_component() -> None:
    """Spec section 7: catalog first, it is prebuilt.

    The component is S4's ProviderCatalog. providers.jsx and
    window.ProvidersPage are deleted by S4 P4 Task 34, which also forbids
    that global anywhere under ui/.
    """
    assert "providers" in _mount_names()
    assert "window.ProviderCatalog" in _host()
    assert "window.ProvidersPage" not in _host()


def test_no_mount_drags_its_old_chrome_in() -> None:
    src = _host()
    for banned in ("window.Chrome", "<nav", "Sidebar", "Topbar"):
        assert banned not in src, banned


def test_the_overlay_is_addressable_and_closable() -> None:
    src = _host()
    assert 'data-testid={"shell-overlay:" + name}' in src
    assert 'data-testid="shell-overlay-close"' in src
    assert "closeOverlay" in src


def test_deep_editing_never_becomes_an_overlay() -> None:
    """Section 8: overlays are for shallow one-decision tasks. A doc kind
    appearing in the mount table would be a comparison surface in a
    modal, which is the thing the rule forbids."""
    kinds = json.loads(MANIFEST.read_text(encoding="utf-8"))["doc_kinds"]
    assert _mount_names().isdisjoint(set(kinds))


def test_the_router_shim_publishes_the_real_contract() -> None:
    """Pinned decision 14: eight reused pages read this hook."""
    src = SHIM.read_text(encoding="utf-8")
    for field in ("path:", "params:", "query:", "navigate:"):
        assert field in src, field
    assert "primerApi" in src
    assert "SH_installRouterShim" in src


def test_the_shim_maps_overlay_segments_onto_params_id() -> None:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis; window.primerApi = {};")
    ctx.eval("var React = {useCallback: function (f) { return f; }};")
    # The shim resolves an overlay NAME against the registry, so the module
    # that owns SH_OVERLAYS loads first here exactly as it does in the
    # browser. Without it every navigate looks like a section path.
    ctx.eval((UI / "foundation" / "shell-url.js").read_text(encoding="utf-8"))
    ctx.eval(SHIM.read_text(encoding="utf-8"))
    out = json.loads(ctx.eval(
        """
        (function () {
          var OV = {name: "providers", section: "tts", id: "pv-1"};
          var seen = [];
          SH_installRouterShim(
            function () { return OV; },
            function (n, s, i) { seen.push([n, s, i]); }
          );
          var r = window.primerApi.useRouter();
          window.primerApi.useRouter().navigate("/providers/llm/pv-9");
          return JSON.stringify([r.path, r.params, seen]);
        })()
        """
    ))
    assert out[0] == "/providers/tts/pv-1"
    assert out[1] == {"id": "pv-1", "section": "tts"}
    assert out[2] == [["providers", "llm", "pv-9"]]


def test_a_lone_trailing_segment_is_the_record_not_the_section() -> None:
    """``navigate("/agents/<id>")`` has to arrive as params.id.

    Every re-hosted page reads params.id; nothing outside the shell
    reads params.section, which is the slot the tab travels in. Filling
    section first meant a page opening its own detail view handed the
    record to a slot its reader never looks at, so the view opened with
    no record and the header had nothing to name.
    """
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis; window.primerApi = {};")
    ctx.eval("var React = {useCallback: function (f) { return f; }};")
    ctx.eval((UI / "foundation" / "shell-url.js").read_text(encoding="utf-8"))
    ctx.eval(SHIM.read_text(encoding="utf-8"))
    out = json.loads(ctx.eval(
        """
        (function () {
          var OV = {name: "agents", section: null, id: null};
          var seen = [];
          SH_installRouterShim(
            function () { return OV; },
            function (n, s, i) { seen.push([n, s, i]); }
          );
          var r = window.primerApi.useRouter();
          r.navigate("/agents/ag-42");
          // A tab still takes the section slot, record alongside it.
          r.navigate("/agents/ag-42?tab=tools");
          return JSON.stringify(seen);
        })()
        """
    ))
    assert out[0] == ["agents", None, "ag-42"]
    assert out[1] == ["agents", "tools", "ag-42"]
