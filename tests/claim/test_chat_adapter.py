from primer.int.claim import ClaimKind
from primer.claim.adapters.chats import ChatClaimAdapter


def test_chat_eligibility_sql_pinned():
    a = ChatClaimAdapter(chat_storage=None)
    sql = a.eligibility_sql()
    assert "status" in sql
    assert "turn_status" in sql
    assert "'claimable'" in sql
    assert "'resumable'" not in sql
    # 'running' is eligible for crash recovery (reclaimed only when the
    # lease has expired); see FINDINGS F9 / ChatClaimAdapter.eligibility_sql.
    assert "'running'" in sql
    assert "parked_status" in sql
