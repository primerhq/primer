import pytest
from matrix.int.claim import ClaimKind, ReleaseOutcome
from matrix.claim.adapters.sessions import SessionClaimAdapter


def test_session_adapter_kind():
    a = SessionClaimAdapter(session_storage=None)
    assert a.kind is ClaimKind.SESSION
    assert a.entity_table == "sessions"


def test_session_eligibility_sql():
    a = SessionClaimAdapter(session_storage=None)
    sql = a.eligibility_sql()
    assert "parked_status IS NULL" in sql
