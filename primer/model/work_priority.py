"""Validated work priority. Public scheduling controls are not enabled yet."""

from typing import Annotated

from pydantic import Field, StrictInt, TypeAdapter

WORK_PRIORITY_MAX = 1_000_000
WorkPriority = Annotated[StrictInt, Field(ge=0, le=WORK_PRIORITY_MAX)]
_VALIDATOR = TypeAdapter(WorkPriority)


def validate_work_priority(value: object) -> int:
    return _VALIDATOR.validate_python(value)
