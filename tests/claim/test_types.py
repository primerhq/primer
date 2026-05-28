import pytest
from datetime import datetime, UTC, timedelta
from primer.int.claim import (
    ClaimKind, Lease, ReleaseOutcome, ClaimAdapter, ClaimEngine,
)


def test_claim_kind_values():
    assert ClaimKind.SESSION.value == "session"
    assert ClaimKind.CHAT.value == "chat"
    assert ClaimKind.HARNESS.value == "harness"


def test_lease_dataclass_fields():
    now = datetime.now(UTC)
    lease = Lease(
        kind=ClaimKind.SESSION,
        entity_id="sess-1",
        claimed_by="worker-1",
        claimed_at=now,
        expires_at=now + timedelta(seconds=60),
        attempt_count=0,
        last_error=None,
    )
    assert lease.kind is ClaimKind.SESSION
    assert lease.entity_id == "sess-1"


def test_release_outcome_defaults():
    outcome = ReleaseOutcome(success=True)
    assert outcome.success is True
    assert outcome.requeue_after is None
    assert outcome.last_error is None
    assert outcome.drop_lease is False


def test_abcs_cannot_be_instantiated():
    with pytest.raises(TypeError):
        ClaimAdapter()
    with pytest.raises(TypeError):
        ClaimEngine()
