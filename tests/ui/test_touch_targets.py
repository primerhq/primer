"""Touch-target guard (new-ui handoff: "44px narrow targets ... are
token-driven via --hit").

The designer's --hit token is the one interactive-control size; this
guard (a) pins its definition and floor, and (b) walks the nv-
console's CSS rules for interactive-control classes and fails any
that size themselves with a raw pixel value below the floor instead
of var(--hit). The enumerated class list grows as wiring phases land.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = (
    Path(__file__).resolve().parents[2] / "ui" / "styles.css"
).read_text(encoding="utf-8")

FLOOR_PX = 32

# Interactive nv- control classes that must size via var(--hit).
# Extend this list in the task that introduces each control.
HIT_SIZED: list[str] = [
    "nv-actbar-btn",
    "nv-avatar",
    "nv-topbar-toggle",
    "nv-composer-iconbtn",
    "nv-stop-btn",
    "nv-send-btn",
    # US-011b (split-view shell cutover): the tab strip and rail shipped
    # with raw 16px/24px icon buttons, under the floor and untracked by
    # this guard until now. DOCUMENTED EXCEPTION, not full compliance:
    # both use calc(var(--hit) - 8px) = 28px (24px compact), below the
    # 32px floor, because their rows are only 34px/30px tall - and the
    # 44px mobile audit (scripts/audit_touch_targets.py) does not cover
    # them either, since neither appears in the max-width:639px block.
    # This guard therefore only pins that their sizing stays token-driven.
    "nv-tg-tab-close",
    "nv-rail-iconbtn",
]


def _rule_bodies(class_name: str) -> list[str]:
    out = []
    for m in re.finditer(
        re.escape("." + class_name) + r"[^{}]*\{([^}]*)\}", CSS
    ):
        out.append(m.group(1))
    return out


def test_hit_token_defined_with_floor():
    m = re.search(r"--hit:\s*(\d+)px", CSS)
    assert m, "--hit must be defined in the base tokens"
    assert int(m.group(1)) >= 36, "comfortable default is 36px+"
    compact = re.search(
        r'data-density="compact"[^{]*\{[^}]*--hit:\s*(\d+)px', CSS
    )
    assert compact and int(compact.group(1)) >= FLOOR_PX, (
        f"compact --hit must stay >= {FLOOR_PX}px"
    )


def test_enumerated_controls_size_via_hit():
    failures = []
    for cls in HIT_SIZED:
        bodies = _rule_bodies(cls)
        if not bodies:
            failures.append(f".{cls}: no rule found")
            continue
        joined = "\n".join(bodies)
        if "var(--hit)" not in joined:
            failures.append(f".{cls}: does not size via var(--hit)")
        for m in re.finditer(
            r"(?:width|height|min-width|min-height):\s*(\d+)px", joined
        ):
            if int(m.group(1)) < FLOOR_PX:
                failures.append(
                    f".{cls}: raw {m.group(1)}px below the {FLOOR_PX}px floor"
                )
    assert not failures, "\n".join(failures)
