from datetime import datetime, timezone

from primer.int.claim import ParkRequest, ReleaseOutcome


def test_release_outcome_defaults_park_to_none():
    outcome = ReleaseOutcome(success=True, drop_lease=True)
    assert outcome.park is None


def test_release_outcome_carries_park_request():
    now = datetime.now(timezone.utc)
    park = ParkRequest(
        parked_state={"schema_version": 1, "yielded": {}},
        parked_event_key="ask_user:sess-1:tc-1",
        parked_until=now,
        parked_at=now,
    )
    outcome = ReleaseOutcome(success=True, drop_lease=True, park=park)
    assert outcome.park is park
    assert outcome.park.parked_event_key == "ask_user:sess-1:tc-1"
    assert outcome.park.parked_until == now


def test_park_request_allows_none_parked_until():
    now = datetime.now(timezone.utc)
    park = ParkRequest(
        parked_state={},
        parked_event_key="k",
        parked_until=None,
        parked_at=now,
    )
    assert park.parked_until is None
