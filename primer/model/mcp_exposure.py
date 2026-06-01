"""Singleton row controlling MCP exposure. Spec §6.

The MCP server endpoint exposes a curated subset of primer's internal
tools to external clients. A single :class:`McpExposure` row in storage
captures the operator's intent: whether the endpoint is live and which
scoped tool ids are allowed through. The row's id is the literal
``"singleton"`` -- there is never more than one.

See ``docs/superpowers/specs/2026-06-02-mcp-server-endpoint-design.md`` §6.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from primer.model.common import Identifiable


class McpExposure(Identifiable):
    """Singleton row -- ``id`` is always ``"singleton"``.

    Fields:

    * ``enabled`` -- master switch. When ``False`` the MCP server endpoint
      refuses every request regardless of the allowlist contents.
    * ``allowed_tools`` -- the operator-managed allowlist of scoped tool
      ids (``toolset_id__tool_id``). Defaults to empty so a freshly
      bootstrapped install exposes nothing until the operator opts in.
    * ``updated_at`` / ``updated_by`` -- audit stamps written on every
      mutation by :func:`primer.mcp.exposure.update_exposure`.
    """

    id: Literal["singleton"] = "singleton"
    enabled: bool = False
    allowed_tools: list[str] = Field(default_factory=list)
    updated_at: datetime
    updated_by: str | None = None


__all__ = ["McpExposure"]
