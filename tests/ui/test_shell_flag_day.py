"""Flag day (spec section 2, decision 2): the fresh shell ships as THE
console and both old shells are deleted in the same release.

The grep-clean assertions are the point. A half-deleted console is worse
than either shell alone, because a stale route silently wins.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
TESTS = ROOT / "tests"

GONE_FILES = [
    "components/chrome.jsx",
    "components/dashboard.jsx",
    "components/studio.jsx",
    "components/studio-activity.jsx",
    "components/studio-center.jsx",
    "components/studio-palette.jsx",
    "components/studio-settings.jsx",
    "components/studio-sidebar.jsx",
    "components/studio-terminal.jsx",
    "foundation/router.js",
]
GONE_DIRS = ["components/studio", "components/studio2"]

# Section 6 plus pinned decision 13. `useRouter` is NOT here: decision 14
# keeps the name alive as a shell-backed shim.
#
# Split by what the token IS. A path can survive as a stale read from a
# test, so those are swept across the test suites too. An identifier can
# only survive where code runs, and a test that asserts a component does
# NOT reach for one has to spell it -- sweeping identifiers across the
# tests would turn every such guard into an offender.
GONE_PATHS = ["chrome.jsx", "studio2"]
GONE_IDENTIFIERS = ["ROUTES", "S2_Shell", "StudioCommandPalette"]
GONE_TOKENS = GONE_PATHS + GONE_IDENTIFIERS

SELF = Path(__file__).resolve()


def _live_sources(*, include_tests: bool = True) -> list[Path]:
    """Everything the grep-clean gate covers: the shipped UI, plus the UI
    test suites when the token is a path. This file is excluded, since it
    necessarily spells the tokens it forbids."""
    out: list[Path] = []
    for pattern in ("*.jsx", "*.js", "*.html"):
        out.extend(UI.rglob(pattern))
    if include_tests:
        out.extend((TESTS / "ui").rglob("*.py"))
        out.extend((TESTS / "ui_e2e").rglob("*.py"))
    # test_shell_legacy_sweep.py is excluded for the same reason as this
    # file: its whole job is to hold the list of doomed names.
    sweep = (TESTS / "ui" / "test_shell_legacy_sweep.py").resolve()
    return sorted(
        p for p in out
        if "fixtures" not in p.parts and p.resolve() not in (SELF, sweep)
    )


@pytest.mark.parametrize("rel", GONE_FILES)
def test_disposable_file_is_gone(rel: str) -> None:
    assert not (UI / rel).exists(), rel


@pytest.mark.parametrize("rel", GONE_DIRS)
def test_disposable_directory_is_gone(rel: str) -> None:
    assert not (UI / rel).exists(), rel


def test_the_router_guard_died_with_its_router() -> None:
    assert not (TESTS / "ui" / "test_sidebar_routes_resolve.py").exists()
    # ...and its successors are present.
    assert (TESTS / "ui" / "test_shell_dual_render_guard.py").is_file()
    assert (TESTS / "ui" / "test_shell_deep_link_guard.py").is_file()


def test_the_studio2_test_set_is_gone() -> None:
    assert not list((TESTS / "ui").glob("test_studio2_*.py"))
    assert not list((TESTS / "ui_e2e").glob("test_studio2_*.py"))


@pytest.mark.parametrize("token", GONE_TOKENS)
def test_no_reference_survives(token: str) -> None:
    # Whole words only: LIST_ROUTES is a test's own fixture list, not a
    # surviving reference to the deleted route table.
    pattern = re.compile(rf"(?<![\w.-]){re.escape(token)}(?![\w])")
    offenders = [
        str(p.relative_to(ROOT))
        for p in _live_sources(include_tests=token in GONE_PATHS)
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"{token} survives in {offenders}"


def test_the_shell_is_mounted_unconditionally() -> None:
    src = (UI / "app.jsx").read_text(encoding="utf-8")
    assert "S2_RootGate" not in src
    assert "SH_RootGate" in src
    assert "AuthGate" in src
    # S5 Task 8 builds SetupWizardGate as a LEAF (onDone only, no children
    # prop, never renders children) and S5 Task 9 owns the setup branch
    # inside AuthGate. Nesting the shell inside the leaf would render the
    # wizard forever, and the old "SetupWizardGate in src" assertion passed
    # on exactly that dead console.
    assert "SetupWizardGate" not in src
    # The page dispatch is gone, so no page-name switch survives.
    assert "const page = (() => {" not in src


def test_the_s7_trace_panel_survived_the_flag_day() -> None:
    """It is props-only and re-hosted by SH_TraceTab; deleting it with the
    studio would orphan the Trace tab."""
    assert (UI / "components" / "shared" / "session-trace.jsx").is_file()
    assert (TESTS / "ui" / "test_session_trace_panel.py").is_file()
    assert not (TESTS / "ui" / "test_session_trace_mount.py").exists()


def test_use_router_has_exactly_one_provider_and_it_is_the_shim() -> None:
    """Pinned decision 14: the name survives, the hash router does not."""
    providers = [
        str(p.relative_to(ROOT))
        for p in _live_sources()
        if re.search(r"\bns\.useRouter\s*=", p.read_text(encoding="utf-8"))
    ]
    assert providers == ["ui/foundation/shell-router-shim.js"], providers


def test_index_html_registers_no_dead_script() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    for match in re.finditer(r'src="([^"]+)"', html):
        rel = match.group(1)
        if rel.startswith("http"):
            continue
        # _app.js is the server-built JSX bundle, served from memory by
        # primer.api._jsx_bundle rather than checked in.
        if rel == "_app.js":
            continue
        assert (UI / rel).exists(), rel
