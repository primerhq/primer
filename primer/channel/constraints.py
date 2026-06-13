"""Channel chat-config validation."""
from __future__ import annotations
from primer.model.channel import ChatConfig

def validate_chat_config(cfg: ChatConfig) -> None:
    """Validate a ChatConfig. The model's own validator enforces the rules
    (default_agent in allowed_agents; default_agent required when enabled);
    this is a named seam routers/handlers can call before persisting.
    Re-validates defensively."""
    ChatConfig.model_validate(cfg.model_dump())

__all__ = ["validate_chat_config"]
