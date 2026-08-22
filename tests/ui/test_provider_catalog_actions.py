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


def test_rows_offer_delete() -> None:
    src = _src()
    assert "function PC_RowActions(" in src
    assert '"DELETE"' in src
    assert 'data-testid="provider-row-delete"' in src


def test_delete_asks_first() -> None:
    """Provider rows are referenced by agents and collections; a
    one-click delete is not recoverable."""
    src = _src()
    assert "confirmDelete" in src


def test_invalidate_is_offered_only_where_the_endpoint_exists() -> None:
    src = _src()
    assert "/invalidate" in src
    marked = re.findall(r'key:\s*"(\w+)"[^}]*invalidate:\s*true', src)
    assert marked == ["llm", "embedding", "cross_encoder"], marked


def test_the_backend_reason_is_shown_inline_not_swallowed() -> None:
    src = _src()
    assert "err.status" in src or "error.status" in src
    assert "detail" in src
    assert 'data-testid="provider-row-error"' in src


def test_no_reserved_id_list_is_duplicated_in_the_console() -> None:
    src = _src()
    for reserved in ("DuckDuckGo", "RESERVED_"):
        assert reserved not in src, (
            f"{reserved} in the catalog duplicates a backend guard"
        )


def test_the_list_is_refetched_after_a_mutation() -> None:
    src = _src()
    assert "refetch()" in src
