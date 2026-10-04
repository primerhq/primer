"""The result of one native token count, with how far it can be trusted."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EstimatedComponent = Literal["system", "tools", "media"]


class TokenCount(BaseModel):
    """A token count plus what stood behind it.

    ``exact`` is True only when the counter is the model family's own
    tokenizer (or a vendor count endpoint) for THIS model; an OpenAI encoding
    applied to a non-OpenAI model, or an aggregated member that may not serve
    the call, is a good approximation and says so. ``estimated_components``
    names parts of the prompt that were estimated rather than counted (media
    blocks, or system/tools for a vendor endpoint that cannot take them), so a
    count is never presented as more certain than it is.
    """

    model_config = ConfigDict(frozen=True)

    total: int = Field(..., ge=0, description="Prompt tokens, estimated parts included.")
    exact: bool = Field(
        ...,
        description=(
            "The counter is the model's own tokenizer or a vendor count "
            "endpoint, not an approximation from another family."
        ),
    )
    estimated_components: tuple[EstimatedComponent, ...] = Field(
        default=(),
        description="Parts of the prompt that were estimated, not counted.",
    )
    encoding: str | None = Field(
        default=None, description="Tokenizer or encoding name, when there is one.",
    )


__all__ = ["EstimatedComponent", "TokenCount"]
