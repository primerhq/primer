"""Entities for the channels feature."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Union

from pydantic import BaseModel, Field, model_validator

from matrix.model.common import Identifiable


class ChannelProviderType(str, Enum):
    SLACK = "slack"
    TELEGRAM = "telegram"
    DISCORD = "discord"


class SlackChannelProviderConfig(BaseModel):
    """Stub — see Spec 3.1 for the real fields."""


class TelegramChannelProviderConfig(BaseModel):
    """Stub — see Spec 3.2 for the real fields."""


class DiscordChannelProviderConfig(BaseModel):
    """Stub — see Spec 3.3 for the real fields."""


ChannelProviderConfig = Annotated[
    Union[
        SlackChannelProviderConfig,
        TelegramChannelProviderConfig,
        DiscordChannelProviderConfig,
    ],
    Field(description="Platform-specific config; must match parent provider."),
]


class ChannelProvider(Identifiable):
    """A configured messaging-platform credential set."""

    provider: ChannelProviderType = Field(...)
    config: ChannelProviderConfig = Field(...)

    @model_validator(mode="before")
    @classmethod
    def _coerce_config_type(cls, values):
        """When ``config`` is a plain dict, construct the right config type.

        Pydantic union parsing always picks the first matching variant
        (``SlackChannelProviderConfig`` for ``{}``) regardless of the
        ``provider`` value. We intercept *before* field parsing so we
        can inject the correct concrete type from the provider name.
        """
        if not isinstance(values, dict):
            return values
        provider_val = values.get("provider")
        config_val = values.get("config")
        if provider_val is None or not isinstance(config_val, dict):
            return values
        # Resolve the provider string (may be an enum value or string).
        pv = provider_val.value if hasattr(provider_val, "value") else str(provider_val)
        cls_map = {
            "slack": SlackChannelProviderConfig,
            "telegram": TelegramChannelProviderConfig,
            "discord": DiscordChannelProviderConfig,
        }
        config_cls = cls_map.get(pv)
        if config_cls is not None:
            values = dict(values)
            values["config"] = config_cls(**config_val)
        return values

    @model_validator(mode="after")
    def _validate_config_matches(self) -> "ChannelProvider":
        match self.provider:
            case ChannelProviderType.SLACK:
                if not isinstance(self.config, SlackChannelProviderConfig):
                    raise ValueError(
                        "provider='slack' requires a SlackChannelProviderConfig"
                    )
            case ChannelProviderType.TELEGRAM:
                if not isinstance(self.config, TelegramChannelProviderConfig):
                    raise ValueError(
                        "provider='telegram' requires a TelegramChannelProviderConfig"
                    )
            case ChannelProviderType.DISCORD:
                if not isinstance(self.config, DiscordChannelProviderConfig):
                    raise ValueError(
                        "provider='discord' requires a DiscordChannelProviderConfig"
                    )
        return self


class Channel(Identifiable):
    """One conversational target within a ChannelProvider."""

    provider_id: str = Field(..., min_length=1)
    external_id: str = Field(..., min_length=1)
    label: str = Field(default="", max_length=200)


class WorkspaceChannelAssociation(Identifiable):
    """Many-to-many link between a workspace and a channel."""

    workspace_id: str = Field(..., min_length=1)
    channel_id: str = Field(..., min_length=1)
    enabled: bool = Field(default=True)
    forward_ask_user: bool = Field(default=True)
    forward_tool_approval: bool = Field(default=True)


__all__ = [
    "Channel",
    "ChannelProvider",
    "ChannelProviderConfig",
    "ChannelProviderType",
    "DiscordChannelProviderConfig",
    "SlackChannelProviderConfig",
    "TelegramChannelProviderConfig",
    "WorkspaceChannelAssociation",
]
