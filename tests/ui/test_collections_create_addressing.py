"""u0025 flake: creating a collection must address its detail overlay
without waiting on a full list refetch to resolve.

CollectionsPage's onCreate bumped reloadKey (to pick up the new row for
the LIST view) and addressed straight into the new row's detail overlay
in the same tick. But useResource keys its cache by reloadKey, so a
bump mints a brand-new (data: undefined) cache entry rather than
refreshing the old one in place - `rows` collapses to [] for the length
of the GET /collections round trip. Addressing (`selectedId`) derived
`addressed` purely via `rows.find(...)`, so the detail overlay rendered
nothing until that fetch resolved - a real, network-latency-bound race
that under CI load could occasionally blow past a Playwright test's
wait budget while every earlier step (create POST, modal close, toast)
stayed reliable, matching the exact symptom this flake reported.

The fix keeps the already-POSTed row around as a fallback for
`addressed` until the list catches up, so the detail overlay has
something to render on the very first paint.

Static-source checks only, matching the rest of the ui/ suite.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "ui" / "components" / "knowledge.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _collections_page_body() -> str:
    src = _src()
    start = src.index("function CollectionsPage(")
    end = src.index("\nwindow.CollectionsPage = CollectionsPage;")
    return src[start:end]


def test_just_created_row_state_exists() -> None:
    body = _collections_page_body()
    assert "React.useState(null)" in body.split("justCreatedRow")[1][:60], (
        "justCreatedRow must default to null"
    )


def test_addressed_falls_back_to_the_just_created_row() -> None:
    """The list-derived lookup must be tried FIRST (so steady-state
    behavior, once the refetch lands, is unchanged) with the
    just-created row only as a fallback for the race window."""
    body = _collections_page_body()
    addressed_block = body.split("const addressed =")[1].split(";\n")[0]
    assert "rows.find((c) => c.id === selectedId)" in addressed_block
    assert "justCreatedRow" in addressed_block
    assert addressed_block.index("rows.find") < addressed_block.index(
        "justCreatedRow"
    ), "the list lookup must be tried before the just-created fallback"


def test_on_create_stamps_just_created_row_before_the_reload_bump() -> None:
    """Order matters only in that both must land before setSelected
    triggers the addressed render - assert both calls exist in the
    onCreate callback, not a stale variant that dropped one."""
    body = _collections_page_body()
    on_create = body.split("onCreate={(row) => {")[1].split("}}")[0]
    assert "setJustCreatedRow(row);" in on_create
    assert "setReloadKey((k) => k + 1);" in on_create
    assert "setSelected(row);" in on_create


def test_on_back_clears_the_just_created_row() -> None:
    """Hygiene: a stale just-created row must not linger past navigating
    back to the list (harmless for a DIFFERENT collection's selectedId,
    but nothing should depend on that instead of an explicit clear)."""
    body = _collections_page_body()
    on_back = body.split("onBack={() => {")[1].split("}}")[0]
    assert "setJustCreatedRow(null);" in on_back
    assert "setSelected(null);" in on_back
