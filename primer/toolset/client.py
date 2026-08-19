"""The server-defined client (browser) toolset: notifying UI actions.

The vocabulary is defined HERE and versioned with the console; no schema
ever travels from the browser (S3 spec decision 6). Every tool in it is
NOTIFYING: the runner answers the call itself and the attached clients
execute it best-effort off the workspace tap, so an agent can act on the
UI without ever being able to hang on it.

``call`` is a no-op acknowledgement: the real execution happens in the
browser. The runner's notifying branch discards this value and returns
its own synthetic success, so the two can never disagree.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, Field

from primer.int.toolset import ToolsetProvider
from primer.model.chat import NOTIFYING_TOOL_RESULT, Tool, ToolCallResult, ToolExample
from primer.model.yield_ import ToolContext
from primer.toolset._describe import make_tool

CLIENT_TOOLSET_ID = "client"


class _OpenFileArgs(BaseModel):
    """Workspace file to open in the user's console."""

    path: str = Field(
        ...,
        min_length=1,
        description="Workspace-relative path of the file to open.",
    )
    line: int | None = Field(
        default=None,
        ge=1,
        description="Optional 1-based line to scroll the viewer to.",
    )


def _client_tools() -> list[Tool]:
    return [
        make_tool(
            id="open_file",
            toolset_id=CLIENT_TOOLSET_ID,
            purpose=(
                "Open a workspace file in the user's console so they can "
                "see it; returns immediately."
            ),
            when=(
                "Use when you want the user to look at a file you just "
                "wrote or found. Delivery is best-effort and you never "
                "wait for it; if nobody has the session open, nothing "
                "opens and the call still succeeds."
            ),
            args_schema=_OpenFileArgs.model_json_schema(),
            examples=[
                ToolExample(
                    args={"path": "config.yaml"},
                    returns='{"delivered": true}',
                    note="notifying; the turn continues immediately",
                ),
                ToolExample(
                    args={"path": "primer/api/app.py", "line": 42},
                    returns='{"delivered": true}',
                ),
            ],
            required_role="user",
            tool_class="notifying",
        ),
    ]


class ClientToolsetProvider(ToolsetProvider):
    """In-memory provider over the client vocabulary. Per-turn grant."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {t.id: t for t in _client_tools()}

    async def list_tools(
        self,
        *,
        principal: str | None = None,
    ) -> AsyncIterator[Tool]:
        del principal  # the vocabulary is server-defined, not per-user
        for tool in self._tools.values():
            yield tool

    def is_yielding(self, tool_name: str) -> bool:
        del tool_name  # notifying tools never park
        return False

    def required_role(self, tool_name: str) -> str:
        # The client toolset only ever rides a turn whose session the
        # caller already has open; any authenticated user rank passes.
        del tool_name
        return "user"

    async def call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        principal: str | None = None,
        ctx: ToolContext | None = None,
    ) -> ToolCallResult:
        """No-op acknowledgement; the attached client does the work."""
        del arguments, principal, ctx
        if tool_name not in self._tools:
            return ToolCallResult(
                output=f"unknown client tool {tool_name!r}", is_error=True
            )
        return ToolCallResult(output=NOTIFYING_TOOL_RESULT, is_error=False)


__all__ = ["CLIENT_TOOLSET_ID", "ClientToolsetProvider"]
