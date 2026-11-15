---
title: "Approvals"
summary: "Admin: gate tool calls behind a person, a rule or a judge model with approval policies."
audience: admin
order: 120
---

These pages are for admins. An approval policy makes a tool call wait for a person, a rule or a judge model before it runs. A gated call pauses the turn, and the decision comes from the console or, when the policy allows it, from a channel. Start with [Approval policies](approvals/policies).

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`tool_approval_policies__find`](reference/tools/tool_approval_policies/find) | Find ToolApprovalPolicy with predicate. | admin |
| [`tool_approval_policies__get_session_tool_approval_pending`](reference/tools/tool_approval_policies/get_session_tool_approval_pending) | get_session_tool_approval_pending. | user |
| [`tool_approval_policies__invalidate`](reference/tools/tool_approval_policies/invalidate) | invalidate. | admin |
| [`tool_approval_policies__list_tool_approval_records`](reference/tools/tool_approval_policies/list_tool_approval_records) | list_tool_approval_records. | user |
| [`tool_approval_policies__post_session_tool_approval_respond`](reference/tools/tool_approval_policies/post_session_tool_approval_respond) | post_session_tool_approval_respond. | user |
| [`workspace_yields__list_pending_attention`](reference/tools/workspace_yields/list_pending_attention) | Aggregated pending attention across all workspaces (Inbox / System dashboard). | user |
| [`workspace_yields__list_pending_yields`](reference/tools/workspace_yields/list_pending_yields) | Aggregated pending yields across all sessions (Studio Action Required). | user |
| [`workspace_yields__list_session_pending_yields`](reference/tools/workspace_yields/list_session_pending_yields) | Pending yields for one session (inline session-stream affordances). | user |
| [`yields__get_ask_user_pending`](reference/tools/yields/get_ask_user_pending) | Get the pending ask_user prompt (404 if none). | user |
| [`yields__post_ask_user_respond`](reference/tools/yields/post_ask_user_respond) | Submit a response to a pending ask_user prompt. | user |
| [`yields__post_cancel_yielded_tool`](reference/tools/yields/post_cancel_yielded_tool) | Cancel one in-flight yield (tool-agnostic). | user |
