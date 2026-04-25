"""Shared base models reused across the configuration schema."""

from pydantic import BaseModel, Field


class Identifiable(BaseModel):
    """Mixin granting a user-defined string identifier.

    Any configuration entry the application needs to address by name should
    inherit from :class:`Identifiable` so the ``id`` field shape stays
    consistent across the schema.
    """

    id: str = Field(
        ...,
        min_length=1,
        description="User-defined identifier referenced by the application.",
    )


class Describeable(Identifiable):
    """Mixin adding a free-form human-readable description to an :class:`Identifiable`.

    Use this for configuration entries that are surfaced to humans (e.g. in
    UIs, logs, or help text) and benefit from a short prose explanation
    alongside their machine identifier.
    """

    description: str = Field(
        ...,
        description="Free-form human-readable description of the entry.",
    )
