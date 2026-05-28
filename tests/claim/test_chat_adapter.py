from primer.int.claim import ClaimKind
from primer.claim.adapters.chats import ChatClaimAdapter


def test_chat_eligibility_sql_pinned():
    a = ChatClaimAdapter(chat_storage=None)
    sql = a.eligibility_sql()
    assert "status" in sql
    assert "turn_status" in sql
    assert "'claimable'" in sql
    assert "'resumable'" in sql
    assert "parked_status" in sql
