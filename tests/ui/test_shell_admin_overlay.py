"""The admin/config overlay: ONE search-first surface (spec section 8).

Common settings at level one, "Advanced" collapsed, admin sections
role-gated, every setting palette-addressable. The search is pure, so it
is executed rather than substring-matched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "shell" / "sh-admin-overlay.jsx"
HOST = UI / "components" / "shell" / "sh-overlay-host.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _sections() -> str:
    m = re.search(r"var SH_ADMIN_SECTIONS = \[([\s\S]*?)\n\];", _src())
    assert m, "the section table must be a literal list"
    return m.group(1)


def test_registered_and_mounted() -> None:
    assert 'src="components/shell/sh-admin-overlay.jsx"' in (
        UI / "index.html"
    ).read_text(encoding="utf-8")
    assert "window.SH_AdminOverlay" in HOST.read_text(encoding="utf-8")


def test_it_is_one_surface_holding_all_four_admin_pages() -> None:
    src = _src()
    for component in ("ADM_AdminUsersPage", "SSO_ProvidersPage",
                      "AT_ApiTokensPage", "MC_McpPage"):
        assert "window." + component in src, component


def test_the_wizard_is_re_hostable_for_re_runs() -> None:
    """Amendment C5: SetupWizardSteps mounts here under the same
    no-chrome contract."""
    assert "window.SetupWizardSteps" in _src()


def test_levels_and_roles_are_declared_per_section() -> None:
    body = _sections()
    assert body.count("level:") == body.count("id:")
    assert body.count("roles:") == body.count("id:")
    assert '"admin"' in body


def test_advanced_is_collapsed_not_a_second_surface() -> None:
    src = _src()
    assert 'data-testid="shell-admin-advanced"' in src
    assert "<details" in src


def _strip_renders(body: str) -> str:
    """Drop every ``render: function ...`` property, whatever its shape."""
    out = body
    while True:
        i = out.find("render: function")
        if i == -1:
            return out
        j = out.index("{", i)
        depth = 0
        for k in range(j, len(out)):
            if out[k] == "{":
                depth += 1
            elif out[k] == "}":
                depth -= 1
                if depth == 0:
                    break
        end = k + 1
        if out[end:end + 1] == ",":
            end += 1
        out = out[:i] + out[end:]


def test_search_is_pure_and_gates_by_role() -> None:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    src = _src()
    body = re.search(
        r"(var SH_ADMIN_SECTIONS = \[[\s\S]*?\n\];)", src
    ).group(1)
    # render() returns JSX, which MiniRacer cannot parse, so the search
    # test evaluates a copy with those properties removed. They come in
    # both one-line and multi-line shapes, so the property is cut by brace
    # balance: a line pattern leaves the multi-line bodies behind, and a
    # non-greedy multi-line pattern swallows whole sections.
    ctx.eval(_strip_renders(body))
    ctx.eval(re.search(
        r"(function SH_searchAdmin\([\s\S]*?\n\})", src
    ).group(1))

    admin_ids = json.loads(ctx.eval(
        'JSON.stringify(SH_searchAdmin(SH_ADMIN_SECTIONS, "", "admin")'
        '.map(function (s) { return s.id; }))'
    ))
    user_ids = json.loads(ctx.eval(
        'JSON.stringify(SH_searchAdmin(SH_ADMIN_SECTIONS, "", "user")'
        '.map(function (s) { return s.id; }))'
    ))
    assert "users" in admin_ids
    assert "users" not in user_ids, "admin sections are role-gated"

    hits = json.loads(ctx.eval(
        'JSON.stringify(SH_searchAdmin(SH_ADMIN_SECTIONS, "token", "admin")'
        '.map(function (s) { return s.id; }))'
    ))
    assert hits == ["api-tokens"]


def test_every_section_is_palette_addressable() -> None:
    """Section 8: every setting palette-addressable. The overlay
    registers one verb per section rather than relying on search."""
    src = _src()
    assert "overlay.open.admin." in src
    assert 'surfaces: ["overlay-button", "palette"]' in src
