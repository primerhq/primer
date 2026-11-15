---
title: "Sessions"
summary: "Start, watch, control and message sessions: the runs of agents and graphs inside a workspace."
order: 30
---

A session is one agent or graph working inside a workspace. These pages say how to start one, watch it, steer or stop it, find sessions in any workspace and message one session from another. Start with [What a session is](sessions/what-a-session-is). To coordinate with another agent, read [Messaging other sessions](sessions/messaging-other-sessions).

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`sessions__find_sessions`](reference/tools/sessions/find_sessions) | Find sessions with predicate. | user |
| [`sessions__get_session_by_id`](reference/tools/sessions/get_session_by_id) | Get session by id (no workspace context required). | user |
| [`sessions__get_session_turn_log`](reference/tools/sessions/get_session_turn_log) | Read the session's per-turn structured log. | user |
| [`sessions__get_session_turn_timeline`](reference/tools/sessions/get_session_turn_timeline) | Derive one turn's execution timeline. | user |
| [`sessions__list_sessions`](reference/tools/sessions/list_sessions) | List sessions across workspaces (optionally filtered). | user |
| [`sessions__recent_sessions`](reference/tools/sessions/recent_sessions) | Recently-active sessions across every live workspace. | user |
| [`workspace_sessions__attach_session`](reference/tools/workspace_sessions/attach_session) | Attach a client to a session (also the heartbeat). | user |
| [`workspace_sessions__compact_session`](reference/tools/workspace_sessions/compact_session) | Compact a session's history into a summary marker. | user |
| [`workspace_sessions__delete_session`](reference/tools/workspace_sessions/delete_session) | Permanently delete a session (auto-cancels non-RUNNING). | user |
| [`workspace_sessions__detach_session`](reference/tools/workspace_sessions/detach_session) | Detach a client from a session. | user |
| [`workspace_sessions__dismiss_pending_message`](reference/tools/workspace_sessions/dismiss_pending_message) | Dismiss one queued follow-up steer. | user |
| [`workspace_sessions__ensure_main_session`](reference/tools/workspace_sessions/ensure_main_session) | Ensure the caller's main operator session on a workspace. | user |
| [`workspace_sessions__rename_session`](reference/tools/workspace_sessions/rename_session) | Rename a session (set its friendly display name). | user |
| [`workspace_sessions__restart_session`](reference/tools/workspace_sessions/restart_session) | Reset an ended session and re-invoke (reset-same-session + wake). | user |
| [`workspace_sessions__rewind_session`](reference/tools/workspace_sessions/rewind_session) | Rewind a session's visible history to a kept user_input. | user |
| [`workspace_sessions__set_session_response_format`](reference/tools/workspace_sessions/set_session_response_format) | Set or clear a session's persistent structured-output schema. | user |
| [`workspace_sessions__switch_session_binding`](reference/tools/workspace_sessions/switch_session_binding) | Switch which agent or graph runs this session's next turn. | user |
