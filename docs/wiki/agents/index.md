---
title: "Agents"
summary: "Define, edit, run and hand off agents: prompts, tool grants, model profiles and compaction."
order: 20
---

An agent is a model profile, a prompt and a list of tools. These pages say how to create one, shape its prompt and tools, run it as a subagent and hand a session to another agent. Start with [What an agent is](agents/what-an-agent-is). Before you give an agent tools, read [Tools and grants](agents/tools-and-grants).

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`agent_memory__get_memory_entry`](reference/tools/agent_memory/get_memory_entry) | One memory entry. | user |
| [`agent_memory__get_memory_settings`](reference/tools/agent_memory/get_memory_settings) | The agent's memory settings. | user |
| [`agent_memory__memory_activity`](reference/tools/agent_memory/memory_activity) | The agent's memory events, newest first (owners only). | user |
| [`agent_memory__patch_memory_entry`](reference/tools/agent_memory/patch_memory_entry) | Edit an entry's description or body, or pin it (owners only). | user |
| [`agent_memory__purge_memory`](reference/tools/agent_memory/purge_memory) | Remove every memory entry; the collection and the settings stay (owners only). | user |
| [`agent_memory__put_memory_settings`](reference/tools/agent_memory/put_memory_settings) | Change the agent's memory settings (owners only). | user |
| [`agents__agent_status`](reference/tools/agents/agent_status) | Validate the agent's external references. | user |
| [`agents__find`](reference/tools/agents/find) | Find Agent with predicate. | user |
| [`agents__get_access`](reference/tools/agents/get_access) | Who Agent is shared with. | user |
| [`agents__put_access`](reference/tools/agents/put_access) | Change who Agent is shared with (owners only). | user |
| [`system__approve_agent_memory_entry`](reference/tools/system/approve_agent_memory_entry) | Approve a candidate memory entry so the agent uses it (it becomes active). | user |
| [`system__archive_agent_memory_entry`](reference/tools/system/archive_agent_memory_entry) | Archive a memory entry so the agent no longer uses it. | user |
| [`system__edit_agent_memory_entry`](reference/tools/system/edit_agent_memory_entry) | Change a memory entry's description or items, or pin it so the curator never merges or archives it. | user |
| [`system__list_agent_memory`](reference/tools/system/list_agent_memory) | List the playbook entries an agent has learned: active, candidates (awaiting approval or a trial run) and archived. | user |
