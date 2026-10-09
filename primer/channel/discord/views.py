"""View + Modal classes for the Discord adapter.

Each Button's ``custom_id`` carries the (verb, workspace_id,
session_id, tool_call_id) tuple verbatim. Discord's 100-char
limit on ``custom_id`` is plenty for short primer IDs. The id of the
gate the button was drawn for (C-033) rides after the tool_call_id as
``#<first 12 characters>`` when that still fits in the limit.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine, Awaitable

import discord
from discord import ButtonStyle, ui

from primer.channel.gate_tag import attach_gate_suffix, split_gate_suffix
from primer.session.gate_token import short_gate_token
import primer.observability.metrics as _metrics


logger = logging.getLogger(__name__)


REJECT_MODAL_CUSTOM_ID_PREFIX = "rm"
"""The reject modal's custom_id verb. It used to be ``primer_reject_modal`` (19 characters), which made the modal's id the longest of the three
and the only one that decided whether the gate token fitted; it is now shorter than ``approve``, so the buttons decide."""

LEGACY_REJECT_MODAL_CUSTOM_ID_PREFIX = "primer_reject_modal"
"""The verb of a modal opened before the rename: still recognised, so a modal left open across a deploy is not dropped."""

_LONGEST_VERB = max(("approve", "reject", REJECT_MODAL_CUSTOM_ID_PREFIX), key=len)


_CUSTOM_ID_MAX = 100
"""Discord's limit on a component's (and a modal's) custom_id."""


def _tcid_with_gate_token(ws: str, sid: str, tcid: str, gate_id: str | None, *, count: bool = True) -> str:
    """``tcid`` with ``#<first 12 characters of the gate id>`` after it, when the LONGEST of the three custom ids still fits in 100 characters.

    The ids are ``<verb>:ws:sid:tcid`` for approve, reject and the reject modal; the longest verb decides for all three (a token on Approve and none
    on Reject would be odd). Without a gate id the id is exactly what it was. A token that would not fit is dropped, which is a decision the
    operator never sees (a click without it is decided as an old button, not fenced), so it is logged and counted (``discord_gate_token_dropped_total``)
    instead of vanishing. The count is per PROMPT, the buttons that were posted: the reject modal is rebuilt on every Reject click and passes ``count=False``.
    """
    token = short_gate_token(gate_id)
    tail = attach_gate_suffix(
        tcid, token, max_len=_CUSTOM_ID_MAX, base_len=len(f"{_LONGEST_VERB}:{ws}:{sid}:"),
    )
    if token is not None and tail == tcid:
        if count:
            _metrics.discord_gate_token_dropped_total.inc()
        logger.warning(
            "discord: the gate token does not fit in a %d-character custom_id (workspace %s, session %s, tool_call_id %d characters); "
            "this prompt's buttons carry no gate token, so a click on them is not fenced against a replaced gate",
            _CUSTOM_ID_MAX, ws, sid, len(tcid),
        )
    return tail


def build_approval_custom_ids(
    *, ws: str, sid: str, tcid: str, gate_id: str | None = None,
) -> tuple[str, str]:
    tail = _tcid_with_gate_token(ws, sid, tcid, gate_id)
    return f"approve:{ws}:{sid}:{tail}", f"reject:{ws}:{sid}:{tail}"


def decode_custom_id(custom_id: str) -> tuple[str, str, str, str] | None:
    """Split a custom_id into (verb, ws, sid, tcid). Returns None
    if the shape doesn't match.
    """
    parts = custom_id.split(":", 3)
    if len(parts) != 4:
        return None
    return parts[0], parts[1], parts[2], parts[3]


def decode_custom_id_with_gate(custom_id: str) -> tuple[str, str, str, str, str | None] | None:
    """Like :func:`decode_custom_id`, with the gate token (C-033) split off the tool_call_id: ``(verb, ws, sid, tcid, token)``.

    ``token`` is ``None`` for a custom id posted before gates had ids.
    """
    parsed = decode_custom_id(custom_id)
    if parsed is None:
        return None
    verb, ws, sid, tail = parsed
    tcid, token = split_gate_suffix(tail)
    return verb, ws, sid, tcid, token


class ApprovalView(ui.View):
    """Persistent view — survives bot restarts via custom_id."""

    def __init__(self, *, ws: str, sid: str, tcid: str, gate_id: str | None = None) -> None:
        super().__init__(timeout=None)
        approve_cid, reject_cid = build_approval_custom_ids(
            ws=ws, sid=sid, tcid=tcid, gate_id=gate_id,
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
    gate_id: str | None = None,
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
        custom_id=f"{REJECT_MODAL_CUSTOM_ID_PREFIX}:{ws}:{sid}:{_tcid_with_gate_token(ws, sid, tcid, gate_id, count=False)}",
    )
    return modal


class _AgentSelect(ui.Select):
    """Single-pick agent dropdown; ``on_pick(interaction, agent_id)`` is
    awaited with the chosen agent id."""

    def __init__(self, *, options, on_pick) -> None:
        self._on_pick = on_pick
        super().__init__(
            placeholder="Pick an agent",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(
                    label=str(o["label"])[:100], value=str(o["agent_id"]),
                )
                for o in options[:25]  # Discord caps a select at 25 options
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._on_pick(interaction, self.values[0])


class AgentSelectView(ui.View):
    """Ephemeral view holding the agent-picker dropdown."""

    def __init__(self, *, options, on_pick, timeout: float = 180.0) -> None:
        super().__init__(timeout=timeout)
        self.add_item(_AgentSelect(options=options, on_pick=on_pick))


__all__ = [
    "AgentSelectView",
    "ApprovalView",
    "LEGACY_REJECT_MODAL_CUSTOM_ID_PREFIX",
    "REJECT_MODAL_CUSTOM_ID_PREFIX",
    "build_approval_custom_ids",
    "build_reject_modal",
    "decode_custom_id",
    "decode_custom_id_with_gate",
]
