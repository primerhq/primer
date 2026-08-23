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
    # seeded empty in P0; P1+ tasks append (composer buttons, topbar
    # controls, activity-bar buttons, ...)
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
