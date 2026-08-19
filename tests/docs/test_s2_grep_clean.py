"""S2 P5 gate: deleted surfaces stay deleted."""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN = [
    "docling",
    "build_search_toolset",
    "_convert_file",
    "DocumentIngester",
    "backfill_missing_document_vectors",
    "_internal_ai_docs",
    "MmrConfig",
]


def _grep(term: str) -> list[str]:
    out = subprocess.run(
        ["grep", "-rIl", term, str(ROOT / "primer"), str(ROOT / "ui")],
        capture_output=True, text=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def test_forbidden_terms_absent_from_engine_and_ui() -> None:
    hits = {t: _grep(t) for t in FORBIDDEN}
    assert all(not v for v in hits.values()), hits
