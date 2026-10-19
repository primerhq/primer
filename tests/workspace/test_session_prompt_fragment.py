"""The workspace system prompt fragment tells the agent how to reach other sessions (pivot P3, plan task 8; design 6.2).

Every session agent gets the fragment; the two sentences it adds name the two messaging tools, the file-over-message rule, and how to read a
peer message's ``[from session <id> (agent <agent_id>)]`` prefix (it is the other session's agent, not the user). The fragment is a function of
the session id alone, so the test renders it directly.
"""

from primer.workspace.session import workspace_system_prompt_fragment


def test_the_fragment_tells_the_agent_how_to_reach_other_sessions():
    text = " ".join(workspace_system_prompt_fragment("ses-1").split())
    assert ("You can see other sessions in this workspace with list_my_sessions and message one with message_session. "
            "Use files for large outputs, messages for coordination.") in text
    assert "A user message that starts with [from session <id> (agent <agent_id>)] was sent by the agent in that session, not by the user." in text
    assert "Use `.state/shared/` to coordinate if you need to." in text
    assert "session `ses-1`" in text
