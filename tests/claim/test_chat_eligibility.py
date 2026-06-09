def test_chat_eligibility_sql_has_no_dead_resumable():
    from primer.claim.adapters.chats import ChatClaimAdapter
    sql = ChatClaimAdapter(chat_storage=None).eligibility_sql()
    assert "resumable" not in sql
    assert "turn_status" in sql and "claimable" in sql and "running" in sql
