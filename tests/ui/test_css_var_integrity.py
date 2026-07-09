"""CSS custom-property integrity.

A recurring class of UI bugs: a component (or a CSS rule) uses `var(--foo)`
where `--foo` is never defined, so it resolves to nothing — a transparent
background, an unstyled surface, or a missing font. Real cases fixed:
  - `--surface`   (profile menu dropdown → see-through, illegible)
  - `--bg-0`      (agent tool-picker sticky header → rows bled through; also
                   several code/panel backgrounds)
  - `--surface-1` (triggers / api-tokens surfaces)
  - `--mono`      (providers row → font var that never existed)

These read as "muddled/illegible" or "can't scroll" to operators. This test
fails if any `var(--NAME)` used across the UI has no definition, so the class
can't come back.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
STYLES = UI / "styles.css"

# Vars supplied at runtime rather than declared in styles.css: React inline
# `style={{ "--x": ... }}` and JS `setProperty("--x", ...)`. Collected below so
# the check stays zero-maintenance, but a couple are only ever written from
# code the regexes below might miss — list those here.
_RUNTIME_ALLOW: set[str] = set()


def _jsx_files() -> list[Path]:
    return sorted(UI.rglob("*.jsx"))


def _defined_vars() -> set[str]:
    defined: set[str] = set()
    css = STYLES.read_text(encoding="utf-8")
    # `--name:` declarations in styles.css
    defined.update(re.findall(r"(--[a-z0-9-]+)\s*:", css))
    # Runtime-provided vars: setProperty("--x", …) and inline style {"--x": …}
    for f in _jsx_files() + [UI / "app.jsx"]:
        src = f.read_text(encoding="utf-8")
        defined.update(re.findall(r"""setProperty\(\s*['"](--[a-z0-9-]+)""", src))
        defined.update(re.findall(r'"(--[a-z0-9-]+)"\s*:', src))
    return defined | _RUNTIME_ALLOW


def _used_vars() -> dict[str, list[str]]:
    """name -> list of 'file:line' where var(--name) is used WITHOUT a fallback.

    `var(--x, default)` is safe even when --x is undefined (it resolves to the
    default), so only bare `var(--x)` usages can produce the transparent/unstyled
    bug this test guards against.
    """
    used: dict[str, list[str]] = {}
    files = _jsx_files() + [STYLES]
    for f in files:
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for name in re.findall(r"var\(\s*(--[a-z0-9-]+)\s*\)", line):
                used.setdefault(name, []).append(f"{f.relative_to(ROOT)}:{i}")
    return used


def test_no_undefined_css_vars_used() -> None:
    defined = _defined_vars()
    used = _used_vars()
    undefined = {name: locs for name, name_locs in used.items()
                 for locs in [name_locs] if name not in defined}
    assert not undefined, (
        "var(--NAME) used with no definition (transparent/unstyled bug):\n"
        + "\n".join(f"  {n} @ {', '.join(locs[:5])}" for n, locs in sorted(undefined.items()))
    )


def test_specific_regressions_stay_fixed() -> None:
    # The exact vars behind reported bugs must never reappear as `var(...)`.
    blob = "\n".join(f.read_text(encoding="utf-8") for f in _jsx_files() + [STYLES])
    for bad in ("var(--surface)", "var(--surface-1)", "var(--bg-0)", "var(--mono)"):
        assert bad not in blob, f"{bad} is undefined and must not be used"
