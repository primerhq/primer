"""T0711 anomaly-banner status guard.

An unreachable / erroring MCP-HTTP toolset's ``GET /toolsets/{id}/tools`` now
returns a proper 5xx (an unreachable upstream is a ``NetworkError`` -> 504,
a bad upstream response a ``ProviderError`` -> 502, ...) rather than the old
leaked 500. The "Tools list unavailable" (T0711) banner must therefore trigger
on the whole server-error class, not a strict ``=== 500`` -- otherwise the
banner silently stops rendering and only the slow, expensive ui-e2e journeys
(U0008 / U0009) catch it. This locks the condition cheaply.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = ROOT / "ui" / "components"


def _src(name: str) -> str:
    return (COMPONENTS / name).read_text(encoding="utf-8")


def test_toolset_tools_banner_triggers_on_5xx_not_only_500():
    src = _src("toolsets.jsx")
    assert "tools.error.status >= 500" in src, (
        "toolset Tools-tab T0711 banner must match the 5xx server-error class"
    )
    # Must not regress to a strict 500 check (unreachable MCP-HTTP is 504).
    assert "tools.error.status === 500" not in src


def test_agent_tools_banner_triggers_on_5xx_not_only_500():
    """uiv2 Wave 2 retarget: this used to be TWO independent per-toolset
    live-fetch T0711 detectors in agents.jsx (the References panel's
    ref-row pill, AND the old read-view Tools tab's own per-toolset
    GET /toolsets/{id}/tools call, purely to show the same detail a
    SECOND time). Wave 2 replaced the read-view tab with the shared
    ToolPicker, which reads the already-aggregated GET /tools catalogue
    instead - the backend's own _catalogue_tools folds a toolset's
    unreachability into available=False + unavailable_reason AT FETCH
    TIME (primer/api/routers/providers.py), so there is no second
    client-side 5xx check to duplicate anymore; only the References
    panel's still does a live per-toolset fetch for its own cross-check
    purpose. This is a real fetch-count reduction (fewer N+1 calls), not
    a dropped capability - ToolPicker still shows unavailable toolsets +
    their reason, plus a Retry action for a total catalogue-fetch
    failure it didn't have before this wave."""
    src = _src("agents.jsx")
    assert src.count("tools.error?.status >= 500") >= 1, (
        "the References panel's per-toolset T0711 detector must match "
        "the 5xx server-error class"
    )
    assert "tools.error?.status === 500" not in src

    picker_src = _src("shared/tool-picker.jsx")
    assert "unavailableToolsets" in picker_src
    assert "unavailable_reason" in picker_src
    assert "catalogue.error" in picker_src and "Retry" in picker_src


def test_t0711_marker_still_present_in_both_surfaces():
    # The tests U0008/U0009 assert a stable "T0711" tag in the rendered banner;
    # keep it in both source files so a copy-edit that drops it fails here too.
    assert "T0711" in _src("toolsets.jsx")
    assert "T0711" in _src("agents.jsx")


def test_bundle_transpiles():
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(ROOT / "ui")
    assert etag and body
