"""Historical home of the CDC kind registry and hook factory.

The registry moved to :mod:`primer.events.registry`; this module
re-exports it under the old names so existing call sites keep working.

The ``make_cdc_hooks`` factory is gone: entity CRUD events now emit
from the storage layer itself and the seeded ``system-cdc`` event
subscription converges the system collection through the
:class:`primer.events.dispatcher.EventDispatcher`
(spec: ``docs/superpowers/specs/2026-08-22-event-bus-design.md``).
The InternalCollectionsSubsystem enqueue path the old hooks also fed
was already dead - its queue has had no worker since the legacy
per-entity namespaces went.
"""

from __future__ import annotations

from primer.events.registry import (
    _reset_for_test,
    known_event_kinds as known_cdc_kinds,
    register_event_kind as register_cdc_kind,
)

__all__ = [
    "register_cdc_kind",
    "known_cdc_kinds",
    "_reset_for_test",
]
