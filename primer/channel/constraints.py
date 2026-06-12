"""Pure validators for the single/multi channel-association constraints.

Single-type channel (no threads, e.g. Telegram): exactly ONE association
total - either a ChatChannelAssociation OR WorkspaceChannelAssociation(s),
never both, at most one.

Multi-type channel (Slack/Discord): at most ONE ChatChannelAssociation plus
any number of WorkspaceChannelAssociations.
"""

from __future__ import annotations

from dataclasses import dataclass

from primer.model.except_ import ConflictError


@dataclass(frozen=True)
class AssociationCounts:
    """Existing-association counts for ONE channel (excluding the new row)."""

    workspace_assocs: int
    chat_assocs: int


def check_chat_association_allowed(
    *, supports_threads: bool, counts: AssociationCounts,
) -> None:
    """Raise ConflictError if a new chat association is not allowed."""
    if supports_threads:
        if counts.chat_assocs >= 1:
            raise ConflictError(
                "multi-type channel already has a chat association "
                "(at most one is allowed)"
            )
        return
    # single-type: no other association of any kind
    if counts.chat_assocs >= 1 or counts.workspace_assocs >= 1:
        raise ConflictError(
            "single-type channel already has an association; a single-type "
            "channel allows exactly one association (chat OR workspace)"
        )


def check_workspace_association_allowed(
    *, supports_threads: bool, counts: AssociationCounts,
) -> None:
    """Raise ConflictError if a new workspace association is not allowed."""
    if supports_threads:
        return  # multi-type: any number of workspace associations
    if counts.chat_assocs >= 1 or counts.workspace_assocs >= 1:
        raise ConflictError(
            "single-type channel already has an association; a single-type "
            "channel allows exactly one association (chat OR workspace)"
        )


__all__ = [
    "AssociationCounts",
    "check_chat_association_allowed",
    "check_workspace_association_allowed",
]
