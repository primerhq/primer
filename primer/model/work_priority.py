"""Validated work priority and the shared REST/tool creation argument."""

from typing import Annotated

from pydantic import BaseModel, Field, StrictInt, TypeAdapter

WORK_PRIORITY_MAX = 1_000_000
WorkPriority = Annotated[StrictInt, Field(ge=0, le=WORK_PRIORITY_MAX)]
_VALIDATOR = TypeAdapter(WorkPriority)


def validate_work_priority(value: object) -> int:
    return _VALIDATOR.validate_python(value)


class SessionPriorityRequest(BaseModel):
    """An omitted priority inherits in a turn. Explicit null is invalid."""

    priority: WorkPriority = Field(
        default_factory=int,
        description="Omit to inherit the calling session's priority, or use 0 on a direct start. "
        "Smaller numbers are more urgent.",
    )

    @property
    def requested_priority(self) -> int | None:
        return self.priority if "priority" in self.model_fields_set else None
