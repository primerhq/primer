"""WCAG contrast guard over the theme token blocks.

The 2026-08-23 audit found gray-on-gray controls that read as disabled.
This test parses the oklch tokens of BOTH themes out of ui/styles.css,
converts them to linear sRGB, and asserts WCAG ratios for every declared
text/surface pair. A token change that breaks legibility fails CI here,
not in a screenshot review.

Only opaque oklch() tokens participate; -dim overlays (alpha < 1) and
shadows are out of scope by design.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

STYLES = Path(__file__).resolve().parents[2] / "ui" / "styles.css"

# (foreground token, background token, minimum ratio). 4.5 = WCAG AA
# normal text. text-3 carries real copy (timestamps, descriptions), so
# it holds the same floor.
PAIRS: list[tuple[str, str, float]] = [
    ("--text", "--bg", 4.5),
    ("--text", "--bg-1", 4.5),
    ("--text", "--bg-2", 4.5),
    ("--text", "--bg-elev", 4.5),
    ("--text-2", "--bg", 4.5),
    ("--text-2", "--bg-1", 4.5),
    ("--text-2", "--bg-2", 4.5),
    ("--text-3", "--bg", 4.5),
    ("--text-4", "--bg", 4.5),
    ("--accent-fg", "--accent", 4.5),
    ("--attention-fg", "--attention", 4.5),
    # The status colours the console draws as text on a surface (the parked chip, a failed tool call, a failed turn, an attachment that
    # did not upload). They were never declared: the stylesheet used `var(--warn, #d9a441)` and `var(--danger, #e06c5f)`, so the light theme
    # got the dark theme's literal at 2.1:1 and 3.0:1 (console review C-030). `--danger` is now an alias of `--red` (declared as `var(--red)`, which this
    # parser does not read), so the danger text is held to AA through `--red`'s own pairs.
    ("--warn", "--bg", 4.5),
    ("--warn", "--bg-1", 4.5),
    ("--warn", "--bg-2", 4.5),
    ("--warn", "--bg-elev", 4.5),
    ("--red", "--bg", 4.5),
    ("--red", "--bg-1", 4.5),
    ("--red", "--bg-2", 4.5),
    ("--red", "--bg-elev", 4.5),
]

_BLOCK_RE = {
    "dark": re.compile(r':root\[data-theme="dark"\]\s*\{(.*?)\}', re.S),
    "light": re.compile(r':root\[data-theme="light"\]\s*\{(.*?)\}', re.S),
}
_DECL_RE = re.compile(
    r"(--[a-z0-9-]+)\s*:\s*oklch\(\s*([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s*\)"
)


def _oklch_to_linear_srgb(L: float, C: float, H: float):
    a = C * math.cos(math.radians(H))
    b = C * math.sin(math.radians(H))
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    r = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    bl = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    clamp = lambda x: min(1.0, max(0.0, x))  # noqa: E731
    return clamp(r), clamp(g), clamp(bl)


def _theme_tokens(theme: str) -> dict[str, tuple[float, float, float]]:
    css = STYLES.read_text(encoding="utf-8")
    block = _BLOCK_RE[theme].search(css)
    assert block, f"theme block for {theme!r} not found in styles.css"
    out: dict[str, tuple[float, float, float]] = {}
    for name, L, C, H in _DECL_RE.findall(block.group(1)):
        out[name] = _oklch_to_linear_srgb(float(L), float(C), float(H))
    return out


def _luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg, bg) -> float:
    la, lb = _luminance(fg), _luminance(bg)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_token_pairs_meet_wcag(theme):
    tokens = _theme_tokens(theme)
    failures = []
    for fg, bg, floor in PAIRS:
        if fg not in tokens or bg not in tokens:
            failures.append(f"{theme}: {fg} or {bg} missing/not plain oklch")
            continue
        ratio = _contrast(tokens[fg], tokens[bg])
        if ratio < floor:
            failures.append(
                f"{theme}: {fg} on {bg} = {ratio:.2f} (needs {floor})"
            )
    assert not failures, "\n".join(failures)


_VAR_WITH_COLOR_FALLBACK_RE = re.compile(r"var\(\s*(--[a-z0-9-]+)\s*,\s*(?:#[0-9a-fA-F]{3,8}|rgba?\(|hsla?\(|oklch\()")
_DECLARED_RE = re.compile(r"(--[a-z0-9-]+)\s*:")


def test_no_color_in_the_stylesheet_depends_on_the_fallback_of_an_undeclared_token():
    """`var(--x, #hex)` with `--x` declared nowhere is the same colour in every theme: it passes review in the dark theme and is wrong in the
    light one (C-030: the parked chip, 2.1:1). A colour token the stylesheet uses with a literal fallback must be declared."""
    css = STYLES.read_text(encoding="utf-8")
    declared = set(_DECLARED_RE.findall(css))
    undeclared = sorted({name for name in _VAR_WITH_COLOR_FALLBACK_RE.findall(css) if name not in declared})
    assert not undeclared, f"colour tokens used with a literal fallback but declared in no theme block: {undeclared}"


_JSX_TOKEN_FALLBACK_RE = re.compile(r"var\(\s*(--[a-z0-9-]+)\s*,\s*var\(")


def test_no_inline_style_depends_on_the_fallback_of_an_undeclared_token():
    """`var(--x, var(--y))` in a .jsx inline style with `--x` declared nowhere always renders `--y`: the intended per-theme colour
    silently never applies. The stylesheet's undeclared-token guard above covers styles.css; this one covers the .jsx inline styles
    (the C-030 follow-up: `--warning` in external-tools.jsx and `--bg-0` in session-detail.jsx both used a token fallback). A token
    used with a token fallback must be declared. Runtime-set vars (for example `--dc-inv-zoom`, set per element via
    `el.style.setProperty`) carry a literal fallback and are out of scope."""
    css = STYLES.read_text(encoding="utf-8")
    declared = set(_DECLARED_RE.findall(css))
    root = STYLES.parent.parent
    offenders = []
    for path in sorted(STYLES.parent.rglob("*.jsx")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for name in _JSX_TOKEN_FALLBACK_RE.findall(line):
                if name not in declared:
                    offenders.append(f"{path.relative_to(root)}:{lineno}: {name}")
    assert not offenders, "inline styles that fall back to a token declared in no theme block:\n" + "\n".join(offenders)
