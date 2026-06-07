"""Single source of truth for e2e test configuration.

Used two ways:
  * the test process imports load_config()/Caps/requires for fixtures + skips
  * scripts/e2e/bringup.sh calls ``python -m tests._support.testconfig
    render-server-config`` to choose the server storage + vector backend
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "tests" / "testconfig.yaml"
_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        def sub(m: "re.Match[str]") -> str:
            return os.environ.get(m.group(1), "")
        return _ENV_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = Path(path) if path is not None else DEFAULT_PATH
    if not path.exists():
        return {"lanes": {"hermetic": True}, "llm": {"mode": "scripted"}}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _interpolate(raw)


class Caps:
    """Which dependencies are available, derived from the config."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self._cfg = cfg
        self._set = self._compute(cfg)

    @staticmethod
    def _truthy(d: dict, *path: str) -> bool:
        cur: Any = d
        for p in path:
            if not isinstance(cur, dict) or p not in cur:
                return False
            cur = cur[p]
        if isinstance(cur, dict):
            return bool(cur.get("enabled"))
        return bool(cur)

    def _compute(self, c: dict[str, Any]) -> set[str]:
        s: set[str] = {"hermetic", "llm:scripted"}  # always available
        st = c.get("server", {}).get("storage", {})
        if st.get("backend") == "postgres" and st.get("postgres_dsn"):
            s.add("postgres")
        vs = c.get("server", {}).get("vector_store", {})
        if vs.get("backend", "lance") in ("pgvector", "pgvectorscale") and vs.get(
            "postgres_dsn"
        ):
            s.add("pgvector")
        if (
            c.get("llm", {}).get("mode") == "real"
            and c.get("llm", {}).get("real", {}).get("base_url")
        ):
            s.add("llm:real")
        if self._truthy(c, "embedder"):
            s.add("embedder")
        if self._truthy(c, "cross_encoder"):
            s.add("cross_encoder")
        for b in ("duckduckgo", "tavily", "exa", "firecrawl"):
            if self._truthy(c, "web_search", b):
                s.add(f"web:{b}")
        for ch in ("slack", "telegram", "discord"):
            if self._truthy(c, "channels", ch):
                s.add(f"channels:{ch}")
        # MCP + harness default to in-repo fixtures: always available unless
        # explicitly pointed elsewhere. The fixture availability is what these
        # caps gate, so they are always present in the hermetic build.
        s.update({"mcp:stdio", "mcp:http", "harness"})
        for wb in ("container", "kubernetes"):
            if self._truthy(c, "workspace_backends", wb):
                s.add(f"workspace:{wb}")
        if self._truthy(c, "lanes", "distributed") and "postgres" in s:
            s.add("distributed")
        return s

    def has(self, dep: str) -> bool:
        return dep in self._set

    def missing(self, deps: tuple[str, ...]) -> list[str]:
        return [d for d in deps if d not in self._set]


_CFG: dict[str, Any] | None = None
_CAPS: Caps | None = None


def caps() -> Caps:
    global _CFG, _CAPS
    if _CAPS is None:
        _CFG = load_config()
        _CAPS = Caps(_CFG)
    return _CAPS


def requires(*deps: str):
    """Pytest decorator: skip the test when any dep is unavailable."""
    import pytest

    missing = caps().missing(deps)
    return pytest.mark.skipif(
        bool(missing),
        reason="testconfig missing capability: " + ", ".join(missing),
    )


def _render_server_config() -> str:
    """Server storage/vector_store override appended to the bringup config.

    Intentionally a no-op. ``scripts/e2e/bringup.sh`` provisions Postgres +
    pgvector itself via ``docker-compose`` (image pgvector/pgvector) and
    renders the matching ``db:`` + ``vector_store:`` AppConfig blocks
    directly, so there is nothing to append here. The testconfig
    ``server.storage`` / ``server.vector_store`` blocks exist only to
    DECLARE the postgres/pgvector capabilities to :class:`Caps` (read from
    the raw config), not to re-render the server. Appending anything here
    would duplicate bringup's blocks and use the stale ``storage:`` /
    ``backend``+``dsn`` schema, which the current AppConfig (``db:`` +
    provider/config) rejects.
    """
    return ""


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "render-server-config":
        sys.stdout.write(_render_server_config())
    else:
        sys.stderr.write(
            "usage: python -m tests._support.testconfig render-server-config\n"
        )
        sys.exit(2)
