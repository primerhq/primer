"""View + Modal classes for the Discord adapter.

Each Button's ``custom_id`` carries the (verb, workspace_id,
session_id, tool_call_id) tuple verbatim. Discord's 100-char
limit on ``custom_id`` is plenty for short matrix IDs.
"""

from __future__ import annotations

from typing import Callable, Coroutine, Awaitable

import discord
from discord import ButtonStyle, ui


REJECT_MODAL_CUSTOM_ID_PREFIX = "matrix_reject_modal"


def build_approval_custom_ids(
    *, ws: str, sid: str, tcid: str,
) -> tuple[str, str]:
    return f"approve:{ws}:{sid}:{tcid}", f"reject:{ws}:{sid}:{tcid}"


def decode_custom_id(custom_id: str) -> tuple[str, str, str, str] | None:
    """Split a custom_id into (verb, ws, sid, tcid). Returns None
    if the shape doesn't match.
    """
    parts = custom_id.split(":", 3)
    if len(parts) != 4:
        return None
    return parts[0], parts[1], parts[2], parts[3]


class ApprovalView(ui.View):
    """Persistent view — survives bot restarts via custom_id."""

    def __init__(self, *, ws: str, sid: str, tcid: str) -> None:
        super().__init__(timeout=None)
        approve_cid, reject_cid = build_approval_custom_ids(
            ws=ws, sid=sid, tcid=tcid,
        )
        self.add_item(ui.Button(
            label="Approve", style=ButtonStyle.success,
            custom_id=approve_cid,
        ))
        self.add_item(ui.Button(
            label="Reject", style=ButtonStyle.danger,
            custom_id=reject_cid,
        ))


def build_reject_modal(
    *,
    ws: str, sid: str, tcid: str,
    on_submit: Callable[[discord.Interaction, str], Awaitable[None]],
) -> ui.Modal:
    """Construct a single-use modal whose custom_id round-trips the IDs."""

    class _RejectModal(ui.Modal, title="Reject tool call"):
        reason = ui.TextInput(
            label="Why are you rejecting?",
            style=discord.TextStyle.long,
            required=True, max_length=1024,
        )

        async def on_submit(self_inner, interaction: discord.Interaction) -> None:  # noqa: N805
            await on_submit(interaction, str(self_inner.reason.value or ""))

    modal = _RejectModal(
        custom_id=f"{REJECT_MODAL_CUSTOM_ID_PREFIX}:{ws}:{sid}:{tcid}",
    )
    return modal


__all__ = [
    "ApprovalView",
    "REJECT_MODAL_CUSTOM_ID_PREFIX",
    "build_approval_custom_ids",
    "build_reject_modal",
    "decode_custom_id",
]
