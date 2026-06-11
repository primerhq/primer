"""Fetch and cache the server's OpenAPI document.

The spec is cached per-context at ``<cache_dir>/<context>/openapi.json`` with a
sidecar ``<context>/openapi.meta.json`` holding the fetch epoch. A cached spec
younger than ``ttl_seconds`` is reused unless ``refresh`` is set.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from primectl.client import ApiClient

SPEC_PATH = "/v1/openapi.json"


def default_cache_dir() -> Path:
    return Path.home() / ".primectl" / "cache"


def _files(cache_dir: Path, context_name: str) -> tuple[Path, Path]:
    base = cache_dir / context_name
    return base / "openapi.json", base / "openapi.meta.json"


def load_spec(
    client: ApiClient,
    *,
    context_name: str,
    cache_dir: Path | None = None,
    refresh: bool = False,
    ttl_seconds: int = 600,
    now: float | None = None,
) -> dict:
    cache_dir = cache_dir or default_cache_dir()
    now = now if now is not None else time.time()
    spec_file, meta_file = _files(cache_dir, context_name)

    if not refresh and spec_file.exists() and meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
            if now - float(meta.get("fetched_at", 0)) < ttl_seconds:
                return json.loads(spec_file.read_text())
        except (ValueError, OSError):
            pass  # fall through to a fresh fetch

    resp = client.request("get", SPEC_PATH)
    spec = resp.json()
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text(json.dumps(spec))
    meta_file.write_text(json.dumps({"fetched_at": now}))
    return spec
