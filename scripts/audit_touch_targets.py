"""Parse ui/styles.css. Inside the (max-width: 639px) block, find every
rule whose selector matches one of the interactive families (.btn,
button:, .nav-item, .fab, .touch-target, .card-interactive, .hamburger,
.chat-mobile-back, .chat-actions-kebab, .mobile-tab) and assert each
rule (or an ancestor selector that applies to the same elements)
declares a min-width AND min-height of at least 44px.

Run as a script: ``uv run python scripts/audit_touch_targets.py``.
Or import in tests and call ``audit()``."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CSS = REPO / "ui" / "styles.css"

INTERACTIVE_SELECTORS = (
    ".btn",
    "button",
    ".nav-item",
    ".fab",
    ".touch-target",
    ".card-interactive",
    ".hamburger",
    ".chat-mobile-back",
    ".chat-actions-kebab",
    ".mobile-tab",
)

TAP_MIN = 44


def _extract_mobile_block(src: str) -> str:
    m = re.search(r"@media\s*\(\s*max-width:\s*639px\s*\)\s*\{", src)
    if not m:
        return ""
    start = m.end()
    depth = 1
    i = start
    while i < len(src) and depth > 0:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[start : i - 1]


def _split_rules(block: str) -> list[tuple[str, str]]:
    rules = []
    pattern = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
    for m in pattern.finditer(block):
        selector = m.group(1).strip()
        body = m.group(2).strip()
        rules.append((selector, body))
    return rules


def _parse_px(value: str) -> int | None:
    m = re.search(r"(\d+)px", value)
    if m:
        return int(m.group(1))
    if "var(--tap-min)" in value:
        return TAP_MIN
    return None


def _rule_covers_tap(body: str) -> bool:
    mw = None
    mh = None
    for line in body.split(";"):
        line = line.strip()
        if line.startswith("min-width"):
            mw = _parse_px(line.split(":", 1)[1])
        elif line.startswith("min-height"):
            mh = _parse_px(line.split(":", 1)[1])
        elif line.startswith("width"):
            mw = max(mw or 0, _parse_px(line.split(":", 1)[1]) or 0) or mw
        elif line.startswith("height"):
            mh = max(mh or 0, _parse_px(line.split(":", 1)[1]) or 0) or mh
    return (mw or 0) >= TAP_MIN and (mh or 0) >= TAP_MIN


def _selector_is_interactive(selector: str) -> bool:
    parts = [p.strip() for p in selector.split(",")]
    for p in parts:
        for tag in INTERACTIVE_SELECTORS:
            if tag in p:
                return True
    return False


def _selector_inherits_touch_target(rules: list[tuple[str, str]]) -> set[str]:
    """Selectors that compose with .touch-target — we treat them as
    inheriting the floor when the same element also carries the
    .touch-target class. The JSX-side audit (separate task) checks
    that consumers attach the class; here we just collect the set."""
    inheriting = set()
    for sel, _ in rules:
        if ".touch-target" in sel:
            inheriting.add(sel)
    return inheriting


def audit_text(css_src: str) -> list[str]:
    failures: list[str] = []
    block = _extract_mobile_block(css_src)
    if not block:
        return ["could not locate @media (max-width: 639px) block"]
    rules = _split_rules(block)
    # The standalone .touch-target rule outside the block also
    # qualifies; allow that to satisfy the class inheritance.
    has_global_touch_target = re.search(
        r"\.touch-target\s*\{[^}]*min-width[^}]*\}", css_src
    )
    for sel, body in rules:
        if not _selector_is_interactive(sel):
            continue
        if _rule_covers_tap(body):
            continue
        if ".touch-target" in sel and has_global_touch_target:
            continue
        failures.append(f"{sel} — declared properties: {body}")
    return failures


def audit() -> list[str]:
    src = CSS.read_text(encoding="utf-8")
    return audit_text(src)


def main() -> int:
    failures = audit()
    if failures:
        print("touch-target audit FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("touch-target audit passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
