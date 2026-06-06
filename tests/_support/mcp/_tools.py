"""Tools shared by the in-repo stdio and http MCP fixture servers.

Both tools are observable: `echo` returns its input verbatim and `bump`
increments a counter file so a test can prove the server actually received a
call (used by the cross-cutting MCP journeys).
"""
from __future__ import annotations

from pathlib import Path


def register(mcp) -> None:
    @mcp.tool()
    def echo(text: str) -> str:
        """Return the given text unchanged."""
        return text

    @mcp.tool()
    def bump(marker_path: str) -> int:
        """Increment an integer counter stored at marker_path; return the new value."""
        p = Path(marker_path)
        current = int(p.read_text(encoding="utf-8")) if p.exists() else 0
        nxt = current + 1
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(nxt), encoding="utf-8")
        return nxt
