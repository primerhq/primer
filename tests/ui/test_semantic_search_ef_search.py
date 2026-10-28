"""G10-3 + G10-7 (audit): the query-time HNSW knob ``hnsw_ef_search``.

The pgvector backend applies it on every session (``SET hnsw.ef_search``,
``primer/vector/pgvector.py``), yet the console's postgres-family SSP form
can neither draw it nor submit it: the HNSW knobs section has M and
ef_construction (both build-time) but no ef_search row, and
``_sspBuildConfig``'s postgres advanced branch omits it (the form state
already carries it via the edit prefill, so nothing is lost on save - it
is just read nowhere and settable nowhere). The lance branch does send it.
G10-7: the docs' ``RESERVED_SSP_IDS`` line and the auto-bootstrap
paragraph predate the reserved ``pgvector`` seed the bootstrap now runs
first on Postgres-mode installs.

MiniRacer over the real ``_sspBuildConfig`` (scalar eval returns), source
scans for the row and the docs.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = (UI / "components" / "semantic-search.jsx").read_text(encoding="utf-8")
DOC = (ROOT / "docs" / "dev" / "subsystems" / "semantic-search.md").read_text(encoding="utf-8")


def _build_config(form_js: str, is_lance: str, is_scale: str, include_advanced: str) -> str:
    from py_mini_racer import MiniRacer

    m = re.search(r"function _sspBuildConfig\(.*?\n\}", SRC, re.S)
    assert m, "_sspBuildConfig not found"
    ctx = MiniRacer()
    ctx.eval(m.group(0))
    out = ctx.eval(
        f"String(_sspBuildConfig({form_js}, {is_lance}, {is_scale}, {include_advanced}).hnsw_ef_search)"
    )
    ctx.close()
    return out


def test_postgres_advanced_submit_carries_hnsw_ef_search() -> None:
    """A postgres-family save with the Advanced section open must submit the query-time
    knob the backend applies per session (the form state carries it, as the edit prefill
    gives it)."""
    assert _build_config(
        "{hostname: 'h', port: 5432, username: 'u', password: 'p', database: 'd', hnsw_ef_search: 200}",
        "false", "false", "true",
    ) == "200"


def test_postgres_core_only_create_still_omits_hnsw_ef_search() -> None:
    """Guard (green pre-fix): an untouched Advanced section sends the core-fields-only
    config - the knob is an advanced field, not a default-view one."""
    assert _build_config(
        "{hostname: 'h', port: 5432, username: 'u', password: 'p', database: 'd', hnsw_ef_search: 200}",
        "false", "false", "false",
    ) == "undefined"


def test_lance_advanced_still_sends_hnsw_ef_search() -> None:
    """Guard (green pre-fix): the lance branch already submits it."""
    assert _build_config("{path: '/x', hnsw_ef_search: 200}", "true", "false", "true") == "200"


def test_the_hnsw_knobs_section_draws_an_ef_search_row() -> None:
    tail = SRC[SRC.index('<Section label="HNSW knobs"'):]
    nxt = tail.find("<Section label=", len('<Section label="HNSW knobs"'))
    block = tail[:nxt] if nxt > 0 else tail
    labels = re.findall(r'FieldRow label="([^"]+)"', block)
    assert "ef_search" in labels, f"HNSW knobs section rows: {labels}"


# G10-7: the reserved-ssp-id contract in the docs.


def test_the_docs_reserved_ssp_ids_line_names_both_ids() -> None:
    lines = [l for l in DOC.splitlines() if "RESERVED_SSP_IDS" in l]
    assert lines, "the RESERVED_SSP_IDS doc line is gone"
    assert "lance" in lines[0] and "pgvector" in lines[0], lines[0]


def test_the_auto_bootstrap_paragraph_names_the_pgvector_seed() -> None:
    lines = DOC.splitlines()
    i = next(k for k, l in enumerate(lines) if "**Auto-bootstrap.**" in l)
    bullet = [lines[i]]
    for l in lines[i + 1:]:
        bullet.append(l)
        if l.startswith("- "):
            break
    text = " ".join(bullet)
    assert "pgvector" in text, text
