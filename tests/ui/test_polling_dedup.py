"""Static checks for FC4 — de-duplicated background polling.

Two endpoints were being polled twice under different useResource cacheKeys,
so the app made two identical requests per cycle. Each pair must now share a
single cacheKey so useResource collapses them into one poll.

  (a) GET /v1/internal_collections/config — app.jsx (sidebar/dashboard) and
      chrome.jsx (topbar bell) must both use the canonical "ic:config" key
      (the same key the Internal Collections page already uses).
  (b) GET /v1/workers — app.jsx (topbar count) and workers.jsx (page list)
      must both use the "workers:list" key.
"""
from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
APP = UI / "app.jsx"
CHROME = UI / "components" / "chrome.jsx"
WORKERS = UI / "components" / "workers.jsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# --- (a) internal_collections/config -----------------------------------

def test_ic_config_shares_one_cache_key() -> None:
    app, chrome = _read(APP), _read(CHROME)
    assert '"ic:config"' in app, "app.jsx IC probe must use the shared 'ic:config' key"
    assert '"ic:config"' in chrome, "chrome.jsx IC bell must use the shared 'ic:config' key"


def test_ic_config_old_split_keys_removed() -> None:
    app, chrome = _read(APP), _read(CHROME)
    assert '"app:ic-config"' not in app, "the split 'app:ic-config' key must be gone"
    assert '"chrome:ic-config"' not in chrome, "the split 'chrome:ic-config' key must be gone"


# --- (b) /v1/workers ---------------------------------------------------

def test_workers_share_one_cache_key() -> None:
    app, workers = _read(APP), _read(WORKERS)
    assert '"workers:list"' in app, "app.jsx topbar must read the shared 'workers:list' key"
    assert '"workers:list"' in workers, "workers.jsx must keep the 'workers:list' key"


def test_workers_old_topbar_key_removed() -> None:
    assert '"topbar:workers"' not in _read(APP), (
        "the separate 'topbar:workers' key must be gone so the two /v1/workers "
        "polls dedupe to one"
    )
