"""Toolset-id extraction from scoped tool ids.

Regression for the ``__`` overload bug: harness rewrites agent tool refs to
3-segment resolved ids ``slug__template__tool``. The toolset row id is the
full ``slug__template`` (everything before the LAST ``__``), not ``slug``.
"""

from __future__ import annotations

from primer.worker.pool import _toolset_ids_from_scoped


def test_three_segment_harness_id_keeps_full_toolset_id():
    assert _toolset_ids_from_scoped(["acme__assistant__search"]) == ["acme__assistant"]


def test_two_segment_id_still_works():
    assert _toolset_ids_from_scoped(["web__http-request"]) == ["web"]


def test_bare_name_skipped():
    assert _toolset_ids_from_scoped(["bare_name"]) == []


def test_compute_extraction_uses_last_separator():
    # Mirror the compute.py agent-status extraction logic.
    def _extract(tool_id: str) -> str:
        if "__" in tool_id:
            return tool_id.rpartition("__")[0]
        return tool_id

    assert _extract("acme__assistant__search") == "acme__assistant"
    assert _extract("web__http-request") == "web"
    assert _extract("bare_name") == "bare_name"
