"""Delete and invalidate, the two actions the deleted detail view had.

The reserved-row guard is NOT re-implemented here: the backend answers
403 with a detail that says why (routers/providers.py:116-135), and the
catalog shows that answer. A second copy of the reserved-id list in the
console would be one more thing to keep in sync.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _src() -> str:
    return (UI / "components" / "provider-catalog.jsx").read_text(encoding="utf-8")


def _form_src() -> str:
    return (UI / "components" / "provider-form.jsx").read_text(encoding="utf-8")


def test_rows_offer_delete() -> None:
    """RETARGET (platform wave P1a item 3): the reference card anatomy
    puts Delete directly on each card's own footer rather than beside a
    single shared form for whichever row happens to be "selected" -
    PC_InstanceCard replaces PC_RowActions, one instance per card, so the
    testid is per-row (provider-card-delete-{id}) rather than a single
    flat one.
    """
    src = _src()
    assert "function PC_InstanceCard(" in src
    assert '"DELETE"' in src
    assert 'data-testid={`provider-card-delete-${row.id}`}' in src


def test_delete_asks_first() -> None:
    """Provider rows are referenced by agents and collections; a
    one-click delete is not recoverable."""
    src = _src()
    assert "confirmDelete" in src


def test_invalidate_is_offered_only_where_the_endpoint_exists() -> None:
    """01a063ab: the Invalidate action itself relocated into the edit
    overlay (PC_InvalidateAction in provider-form.jsx) - the card footer
    stays mockup-pure (Open/Delete only) per the lead's ruling, so the
    capability moved rather than being dropped. Which classes offer it
    (invalidate: true on PROVIDER_CLASSES) is unchanged and still lives
    on the catalog."""
    src = _src()
    marked = re.findall(r'key:\s*"(\w+)"[^}]*invalidate:\s*true', src)
    assert marked == ["llm", "embedding", "cross_encoder"], marked
    assert "/invalidate" in _form_src()


def test_the_backend_reason_is_shown_inline_not_swallowed() -> None:
    src = _src()
    # RETARGET (01a063ab): the delete catch variable was renamed from the
    # generic `err` (which would have shadowed the sibling error-message
    # state variable of the same name) to `deleteErr`.
    assert "deleteErr.status" in src
    assert "detail" in src
    assert 'data-testid={`provider-card-error-${row.id}`}' in src


def test_no_reserved_id_list_is_duplicated_in_the_console() -> None:
    src = _src()
    for reserved in ("DuckDuckGo", "RESERVED_"):
        assert reserved not in src, (
            f"{reserved} in the catalog duplicates a backend guard"
        )


def test_the_list_is_refetched_after_a_mutation() -> None:
    src = _src()
    assert "refetch()" in src
