"""ReleaseOutcome.entity_update and claim_token (Phase 3 stage 7a, slice S1-B).

The fields are inert data on the outcome; what an adapter does with them is tested with the adapter
(test_tool_call_adapter.py). Here: they default to "absent", and no other adapter's release changes.
"""

from __future__ import annotations

import dataclasses

import pytest

from primer.int.claim import ReleaseOutcome


def test_both_fields_default_to_absent_so_every_existing_release_is_unchanged():
    outcome = ReleaseOutcome(success=True)
    assert outcome.entity_update is None
    assert outcome.claim_token is None


def test_the_outcome_is_still_frozen_and_carries_the_new_fields_through_replace():
    outcome = ReleaseOutcome(success=True, drop_lease=True, claim_token="tok", entity_update={"attempts": 2})
    assert (outcome.claim_token, outcome.entity_update) == ("tok", {"attempts": 2})
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.claim_token = "other"  # type: ignore[misc]
    assert dataclasses.replace(outcome, success=False).entity_update == {"attempts": 2}


def test_a_lease_only_release_cannot_carry_a_fenced_entity_write():
    """entity_noop never reaches an adapter, so an entity_update (or the claim_token that fences one) would be
    silently dropped: refused at construction instead, with the existing park rules unchanged."""
    ok = ReleaseOutcome(success=True, entity_noop=True, drop_lease=True)
    assert ok.entity_noop and ok.entity_update is None and ok.claim_token is None
    with pytest.raises(ValueError, match="entity_update or a claim_token"):
        ReleaseOutcome(success=True, entity_noop=True, entity_update={"attempts": 1})
    with pytest.raises(ValueError, match="entity_update or a claim_token"):
        ReleaseOutcome(success=True, entity_noop=True, claim_token="tok")
    with pytest.raises(ValueError, match="park or preserve_park"):
        ReleaseOutcome(success=True, entity_noop=True, preserve_park=True)
    # the same fields without entity_noop are fine, and an empty update is not "an update"
    assert ReleaseOutcome(success=True, entity_update={"attempts": 1}, claim_token="tok").claim_token == "tok"
    assert ReleaseOutcome(success=True, entity_noop=True, entity_update={}).entity_noop


@pytest.mark.asyncio
async def test_a_release_carrying_the_fields_reaches_a_non_tool_call_adapter_untouched():
    """The session, harness and trigger adapters ignore both fields: passing them is not an error."""
    from primer.claim.adapters.harnesses import HarnessClaimAdapter
    from primer.claim.in_memory import InMemoryClaimEngine
    from primer.int.claim import ClaimKind

    seen: list[ReleaseOutcome] = []

    class _Spy(HarnessClaimAdapter):
        async def on_release(self, conn, entity_id, *, outcome):
            seen.append(outcome)

    engine = InMemoryClaimEngine(adapters={ClaimKind.HARNESS: _Spy(harness_storage=None)})
    await engine.upsert(ClaimKind.HARNESS, "h-1")
    (lease,) = await engine.claim_due("w", max_count=1)
    outcome = ReleaseOutcome(success=True, drop_lease=True, claim_token="tok", entity_update={"k": 1})
    await engine.release(lease, outcome=outcome)

    assert seen == [outcome]
    assert await engine.has_lease(ClaimKind.HARNESS, "h-1") is False
