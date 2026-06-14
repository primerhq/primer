"""Source registry — maps TriggerKind to its source implementation."""

from __future__ import annotations

from primer.trigger.sources.delayed import DelayedSource
from primer.trigger.sources.scheduled import ScheduledSource
from primer.trigger.sources.webhook import WebhookSource


SOURCES: dict[str, object] = {
    "delayed": DelayedSource(),
    "scheduled": ScheduledSource(),
    "webhook": WebhookSource(),
}


def get_source(kind: str):
    if kind not in SOURCES:
        raise KeyError(f"unknown trigger kind: {kind!r}")
    return SOURCES[kind]


__all__ = ["SOURCES", "get_source"]
