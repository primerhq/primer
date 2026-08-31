"""S7 P4: the subsystem doc covers what S7 added.

The doc is UPDATED, never replaced (2026-08-15 amendment): it already
documents this surface in eight sections.
"""
from __future__ import annotations

from pathlib import Path

DOC = (
    Path(__file__).resolve().parents[2]
    / "docs" / "dev" / "architecture" / "observability.md"
)


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_documents_every_gap_instrument() -> None:
    text = _text()
    for name in (
        "worker_tasks_total",
        "worker_task_duration_seconds",
        "turns_total",
        "turn_duration_seconds",
        "llm_calls_total",
        "llm_profile_tokens_total",
        "sessions_active",
    ):
        assert name in text, f"{name} undocumented"


def test_documents_the_label_allowlist() -> None:
    text = _text()
    assert "ALLOWED_LABEL_NAMES" in text
    assert "session_id" in text


def test_documents_the_stable_worker_label() -> None:
    text = _text()
    assert "stable_worker_label" in text
    assert "primer/worker/identity.py" in text


def test_documents_the_llm_call_record_and_timeline() -> None:
    text = _text()
    assert "llm_call" in text
    assert "/turns/{turn_no}/timeline" in text
    assert "primer/session/timeline.py" in text


def test_documents_otlp_metrics_as_collector_scrape() -> None:
    text = _text()
    assert "scrape" in text.lower()
    assert "otlp.metrics" not in text


def test_keeps_the_existing_section_structure() -> None:
    text = _text()
    for heading in (
        "## 1. Purpose",
        "## 3. Public surface",
        "## 5. Existing implementations",
        "## 6. Wiring",
        "## 7. Testing patterns",
        "## 8. Historical decisions",
    ):
        assert heading in text, f"{heading} lost"


def test_no_em_dash() -> None:
    assert "\u2014" not in _text()
