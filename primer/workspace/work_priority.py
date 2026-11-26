"""Choose a fresh session's priority from the server-resolved creation context."""

from __future__ import annotations

from typing import TYPE_CHECKING

from primer.model.except_ import AccessDeniedError, ConfigError
from primer.model.trigger import TriggerKind
from primer.model.work_priority import validate_work_priority
from primer.model.workspace_session import WorkspaceSession

if TYPE_CHECKING:
    from primer.trigger.subscribers import TriggerFireMetadata


def choose_creation_priority(
    *, requested: int | None, caller: WorkspaceSession | None, fire: TriggerFireMetadata | None,
    binding_default: int,
) -> int:
    if fire is not None:
        if requested is not None or caller is not None:
            raise ConfigError("a fresh-session trigger cannot also be a caller-directed start")
        interactive = fire.kind in (TriggerKind.WEBHOOK, TriggerKind.CHANNEL) and fire.interactive
        priority = 0 if interactive else validate_work_priority(binding_default)
    else:
        floor = caller.priority if caller is not None else 0
        priority = floor if requested is None else validate_work_priority(requested)
        if priority < floor:
            raise AccessDeniedError("child priority cannot be more urgent than its calling session")
    return priority
