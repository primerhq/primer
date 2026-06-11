import json
from pathlib import Path

import httpx
import pytest

from primectl.client import ApiClient
from primectl.discovery import load_spec


def _client(calls: list, payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=payload)

    return ApiClient(server="http://test", transport=httpx.MockTransport(handler))


def test_fetches_and_caches(tmp_path: Path):
    calls: list[str] = []
    client = _client(calls, {"openapi": "3.1.0", "paths": {}})
    spec = load_spec(
        client, context_name="t", cache_dir=tmp_path, refresh=False, ttl_seconds=600,
    )
    assert spec["openapi"] == "3.1.0"
    assert calls == ["/v1/openapi.json"]
    cache_file = tmp_path / "t" / "openapi.json"
    assert cache_file.exists()


def test_second_call_uses_cache(tmp_path: Path):
    calls: list[str] = []
    client = _client(calls, {"openapi": "3.1.0", "paths": {}})
    load_spec(client, context_name="t", cache_dir=tmp_path, ttl_seconds=600)
    load_spec(client, context_name="t", cache_dir=tmp_path, ttl_seconds=600)
    assert len(calls) == 1  # second call served from cache


def test_refresh_forces_fetch(tmp_path: Path):
    calls: list[str] = []
    client = _client(calls, {"openapi": "3.1.0", "paths": {}})
    load_spec(client, context_name="t", cache_dir=tmp_path, ttl_seconds=600)
    load_spec(client, context_name="t", cache_dir=tmp_path, ttl_seconds=600, refresh=True)
    assert len(calls) == 2
