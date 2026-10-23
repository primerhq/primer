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
    src = _src()
    assert "pc-card-delete" in src
    assert '"DELETE"' in src


def test_delete_asks_first() -> None:
    src = _src()
    assert "confirmDialog(" in src


def test_invalidate_is_offered_only_where_the_endpoint_exists() -> None:
    common = (UI / "components" / "providers" / "register-common.jsx").read_text(encoding="utf-8")
    marked = re.findall(r'key:\s*"(\w+)"[^}]*invalidate:\s*true', common)
    assert marked == ["llm", "embedding", "cross_encoder"], marked
    assert "/invalidate" in _form_src()


def test_the_backend_reason_is_shown_inline_not_swallowed() -> None:
    src = _src()
    assert "readRefusal" in src
    assert "Delete refused" in src


def test_no_reserved_id_list_is_duplicated_in_the_console() -> None:
    """provider-catalog.jsx holds no reserved id, and the one built-in list equals the server's (tests/ui/test_provider_builtin_ids_match_server.py)."""
    src = _src()
    for reserved in ("DuckDuckGo", "RESERVED_"):
        assert reserved not in src, (
            f"{reserved} in the catalog duplicates a backend guard"
        )


def test_the_list_is_refetched_after_a_mutation() -> None:
    src = _src()
    assert "refetch()" in src
