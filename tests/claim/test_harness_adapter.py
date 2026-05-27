from matrix.int.claim import ClaimKind
from matrix.claim.adapters.harnesses import HarnessClaimAdapter


def test_harness_eligibility_sql():
    a = HarnessClaimAdapter(harness_storage=None)
    sql = a.eligibility_sql()
    assert "pending_operation" in sql
    assert "IS NOT NULL" in sql
