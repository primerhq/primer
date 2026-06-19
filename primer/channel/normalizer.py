from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from primer.model.channel import ChannelProviderType
from primer.model.channel_event import ChannelEvent, NormalizedEventType


class ProviderCapabilities(BaseModel):
    provider: ChannelProviderType
    supported: set[NormalizedEventType]
    prerequisites: dict[str, str] = Field(default_factory=dict)


@runtime_checkable
class ChannelEventNormalizer(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    async def normalize(self, raw) -> ChannelEvent | None: ...


__all__ = ["ChannelEventNormalizer", "ProviderCapabilities"]
