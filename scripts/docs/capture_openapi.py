"""Dump the live OpenAPI schema for use as the docs API source of truth.

Run against a live server:
  PRIMER_BASE=http://127.0.0.1:8000 uv run python scripts/docs/capture_openapi.py
"""
import json
import os
import urllib.request

base = os.environ.get("PRIMER_BASE", "http://127.0.0.1:8000")
out = "tests/_docs_fixtures/openapi.json"
os.makedirs("tests/_docs_fixtures", exist_ok=True)
with urllib.request.urlopen(f"{base}/v1/openapi.json", timeout=20) as r:
    spec = json.load(r)
with open(out, "w") as f:
    json.dump(spec, f, indent=2, sort_keys=True)
print(f"wrote {out}: {len(spec.get('paths', {}))} paths")
