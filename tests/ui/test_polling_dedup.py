"""Static checks for FC4 -- de-duplicated background polling.

Two endpoints were being polled twice under different useResource cacheKeys,
so the app made two identical requests per cycle. The fix was to give each
endpoint ONE canonical key that every consumer shares.

Stated repo-wide rather than against a fixed pair of files: consumers come
and go as surfaces move (the topbar bell became a shell surface), but the
invariant is the same wherever the poll lives -- one endpoint, one key.
"""
from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"

# endpoint -> (canonical cacheKey, the split keys it replaced)
CANONICAL = {
    "/v1/internal_collections/config": ("ic:config", ("app:ic-config", "chrome:ic-config")),
    "/v1/workers": ("workers:list", ("topbar:workers",)),
}


def _sources() -> list[Path]:
    return sorted(p for p in UI.rglob("*.js*") if p.is_file())


def test_no_split_cache_keys_survive() -> None:
    for endpoint, (canonical, retired) in CANONICAL.items():
        for dead in retired:
            hits = [
                str(p.relative_to(UI)) for p in _sources()
                if f'"{dead}"' in p.read_text(encoding="utf-8")
            ]
            assert hits == [], (
                f"{endpoint} must poll under the single {canonical!r} key; "
                f"the split {dead!r} key is still read by: {hits}"
            )


def test_canonical_keys_are_still_in_use() -> None:
    """A key nothing reads is a dedup that quietly stopped applying."""
    for endpoint, (canonical, _retired) in CANONICAL.items():
        hits = [
            str(p.relative_to(UI)) for p in _sources()
            if f'"{canonical}"' in p.read_text(encoding="utf-8")
        ]
        assert hits, f"no consumer reads {endpoint} under {canonical!r}"
