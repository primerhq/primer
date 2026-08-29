"""Toolset detail 'Tools' tab: y/w/r/n capability badges.

Follow-up to the R4 Workbench turn: GET /toolsets/{id}/tools did not carry
yields/requires_workspace/tool_class/required_role (unlike GET
/tools/catalogue and GET /tools, batch-2 item 1), so TS_ToolsTab could not
have rendered CapabilityBadges honestly. The backend route now adds the
same 4 fields back with the same getattr pattern; this pins that the tab
renders them once they are real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
TOOLSETS = UI / "components" / "toolsets.jsx"
PROVIDERS_ROUTER = ROOT / "primer" / "api" / "routers" / "providers.py"
TOOLS_ROUTER = ROOT / "primer" / "api" / "routers" / "tools.py"
MCP_EXPOSURE = ROOT / "primer" / "mcp" / "exposure.py"

BADGE_FIELDS = ("yields", "requires_workspace", "tool_class", "required_role")


def _src() -> str:
    return TOOLSETS.read_text(encoding="utf-8")


def _function_body(path: Path, def_marker: str) -> str:
    """Slice one function's source, from its def line to the next
    top-level def/decorator (or end of file if it's the last one)."""
    src = path.read_text(encoding="utf-8")
    start = src.index(def_marker)
    search_from = start + 1
    candidates = []
    for marker in ("\nasync def ", "\ndef ", "\n@"):
        idx = src.find(marker, search_from)
        if idx >= 0:
            candidates.append(idx)
    end = min(candidates) if candidates else len(src)
    return src[start:end]


def test_tools_tab_renders_capability_badges() -> None:
    src = _src()
    start = src.index("function TS_ToolsTab(")
    end = src.index("\nfunction ", start + 1)
    body = src[start:end]
    assert "<CapabilityBadges tool={tool}" in body


def test_capabilities_column_header_present() -> None:
    src = _src()
    assert "<th>capabilities</th>" in src


@pytest.mark.parametrize(
    "label,path,def_marker",
    [
        (
            "GET /toolsets/{id}/tools",
            PROVIDERS_ROUTER, "async def list_toolset_tools(",
        ),
        (
            "GET /tools (via _catalogue_tools)",
            PROVIDERS_ROUTER, "async def _catalogue_tools(",
        ),
        (
            "POST /toolsets/{id}/validate",
            PROVIDERS_ROUTER, "async def validate_python_source(",
        ),
        (
            "GET /toolsets/{id}/runtime",
            PROVIDERS_ROUTER, "async def toolset_runtime(",
        ),
        (
            "GET /tools/catalogue",
            TOOLS_ROUTER, "async def list_tools(",
        ),
        (
            "GET /available (MCP allowlist)",
            MCP_EXPOSURE, "async def list_available_tools(",
        ),
    ],
)
def test_every_tool_serialising_endpoint_carries_the_four_fields(
    label: str, path: Path, def_marker: str,
) -> None:
    """R4 review finding 1: validate_python_source and toolset_runtime
    only re-added "yields", the same class of drift
    test_backend_route_now_carries_the_four_fields (this file's earlier,
    single-endpoint version) already caught once for list_toolset_tools.
    Parametrized across all SIX tool-serialising routes (the five
    original REST ones plus the MCP allowlist, which used to be the
    exception that carried none of the four - platform wave P2, #28)
    so a future sibling endpoint can't regress this a fourth time
    silently - one always-empty badge row would be worse than none.

    Platform wave P2 (#28) centralized the four field names behind
    Tool.catalogue_flags -- primer/model/chat.py's tool_catalogue_flags()
    -- so none of these endpoints spells "yields"/"requires_workspace"/
    "tool_class"/"required_role" literally any more; every one of them
    calls the shared helper instead (see providers.py/tools.py's own
    "one seam" comments at each call site). Retargeted to assert THAT
    call is present, source-level, rather than the literal field names
    the refactor deliberately removed - the wire-shape half of this
    invariant (the four keys actually reach the response) is pinned by
    the real API tests instead: tests/api/test_python_toolset_routes.py,
    tests/mcp/test_exposure_service.py, and
    tests/model/test_tool_catalogue_flags.py.
    """
    body = _function_body(path, def_marker)
    assert "tool_catalogue_flags(" in body, (
        f"{label}: does not call tool_catalogue_flags() - "
        f"the badge fields {BADGE_FIELDS} would be dropped"
    )


def test_bundle_transpiles_with_the_tools_tab_badges() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    _etag, body = build_jsx_bundle(UI)
    text = body.decode("utf-8")
    assert "toolset-tool-badges-" in text
