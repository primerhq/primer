"""``Modal`` has no ``open`` prop, so no call site may pretend it does.

``Modal`` (components/shared.jsx) renders whenever it is mounted; visibility
is the caller's job, spelled ``{cond && <Modal ...>}`` on every page that has
a confirm dialog. Passing ``open={cond}`` instead looks right and type-checks
nowhere, so the prop is silently dropped and the dialog renders permanently.

That shipped on the model profiles page: the delete confirm appeared on page
load with a blank id (no row was selected, so ``confirmDelete?.id`` was
undefined) and Cancel could not dismiss it, because closing only reset state
the modal never consulted.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def test_modal_signature_has_no_open_prop() -> None:
    """Pin the premise. If Modal ever gains `open`, this test must be revisited."""
    src = (UI / "components" / "shared.jsx").read_text(encoding="utf-8")
    sig = re.search(r"const Modal = \(\{([^}]*)\}\)", src)
    assert sig, "Modal signature not found in shared.jsx; update this test"
    props = {p.strip() for p in sig.group(1).split(",")}
    assert "open" not in props, (
        "Modal now accepts `open`; either it gates itself (drop this test) or "
        "the callers below are no longer wrong"
    )


def _modal_call_sites_passing_open() -> list[str]:
    """Every `<Modal ... open=...>` opening tag, as `path:line`."""
    hits = []
    for path in sorted(UI.rglob("*.jsx")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not re.search(r"<Modal\b", line):
                continue
            # Props are one per line; the opening tag ends on a bare ">".
            for j in range(i, min(i + 40, len(lines))):
                stripped = lines[j].strip()
                if j > i and stripped in (">", "/>"):
                    break
                if re.match(r"^open=", stripped):
                    hits.append(f"{path.relative_to(UI)}:{j + 1}")
                    break
    return hits


def test_no_caller_passes_open_to_modal() -> None:
    offenders = _modal_call_sites_passing_open()
    assert not offenders, (
        "Modal ignores `open`, so these dialogs render unconditionally. Gate "
        f"them with {{cond && <Modal ...>}} instead: {offenders}"
    )


def test_model_profiles_delete_confirm_is_gated() -> None:
    """The dialog that regressed, pinned directly."""
    src = (UI / "components" / "model-profiles.jsx").read_text(encoding="utf-8")
    assert "{confirmDelete && (" in src
    # Inside the guard the row is present, so no optional chaining is needed;
    # `confirmDelete?.id` was what rendered the tell-tale blank "Delete ?".
    assert "confirmDelete?.id" not in src
