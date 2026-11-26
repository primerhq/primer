"""A stand-in adapter for a kind a test does not model.

Arming is always on: every upserted kind needs a registered adapter. These
answer the base platform-work contract (priority 0, always eligible) for a
kind whose entity the test does not store, so engine bookkeeping can be
exercised without entity storage.
"""

from __future__ import annotations

from primer.int.claim import ClaimAdapter, ClaimKind, ReleaseOutcome


class _Platform(ClaimAdapter):
    entity_table = "platform"

    def eligibility_sql(self) -> str:
        return "TRUE"

    async def on_release(self, conn, entity_id: str, *, outcome: ReleaseOutcome) -> None:
        return None


def platform_adapter(kind: ClaimKind) -> ClaimAdapter:
    return platform_adapters(kind)[kind]


def platform_adapters(*kinds: ClaimKind) -> dict[ClaimKind, ClaimAdapter]:
    return {
        kind: type(f"_Platform_{kind.name}", (_Platform,), {"kind": kind, "entity_table": kind.value})()
        for kind in kinds
    }
