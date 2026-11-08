---
title: "Build a wiki-driven agent"
summary: "Create an agent whose prompt teaches the wiki loop, grant it the core kit, and verify it is wiki-first with the characterization probe."
order: 8
tools: [system__create_agent, workspaces__create_workspace_session, workspaces__read_workspace_session_messages]
related: [knowledge/the-platform-wiki, start-here/the-core-kit, start-here/reading-this-wiki, agents/create-and-edit]
---
## Goal

Build an agent that resolves every "how do I do X" from the platform wiki before acting: the purpose-mapped index, then a wiki search, then the page, then the exact scoped tool the page names, then the approval gate where a write requires one. This is the wiki-primary execution pattern as a buildable recipe: the prompt teaches HOW TO FIND (the loop, short), and the wiki carries WHAT EXISTS (capabilities, workflows, exact scoped tool ids, approval gates, verifiable end states).

## Prerequisites

- A working model profile ([runbooks/configure-provider](runbooks/configure-provider)).
- A workspace to run the agent in ([workspaces/create-and-delete](workspaces/create-and-delete)).
- Nothing else: the wiki is built at boot into the `system` collection and is always present ([the platform wiki](knowledge/the-platform-wiki)).

## Steps

### 1. Create the agent with the loop prompt

Create the row with `system__create_agent`. Grant the smallest tool surface that does the job: the core-kit scoped ids (the door plus the wiki), and put the grounding rule in `system_prompt`. The prompt teaches the loop and nothing about what exists: no capability text, no tool inventories, no workflows.

```json
{"toolset_id": "system", "tool_name": "create_agent", "arguments": {"entity": {"id": "wiki-runner", "description": "A wiki-driven worker agent.", "model": {"profile_id": "my-profile"}, "tools": ["system__call_tool", "system__wiki_read", "system__wiki_search"], "system_prompt": ["Grounding rule: resolve the action from the platform wiki before you act, in this order: the purpose-mapped index (wiki path purpose-map) maps a goal to the page that covers it; wiki_search finds a page from a description when the index does not name one; wiki_read opens a page by its path. A page names the exact scoped tools the flow uses and the approval gates on its writes: make those calls with call_tool and stop at an approval gate. For a documented flow, never explore the raw REST API or probe the database for the mechanism: if the wiki has no page for the flow, say the flow is not documented and ask."]}}}
```

The core kit stays small on purpose: `system__call_tool` is the door to every built-in tool, so the grant does not grow with the agent's ambitions ([the core kit](start-here/the-core-kit)).

### 2. Start a session with a real task

Bind the agent and start its first turn with `workspaces__create_workspace_session`, giving it a goal a documented flow covers (a runbook, a configuration flow):

```json
{"toolset_id": "workspaces", "tool_name": "create_workspace_session", "arguments": {"workspace_id": "ws-9f2c", "binding": {"kind": "agent", "agent_id": "wiki-runner"}, "initial_instructions": "Configure the LLM provider from the wiki runbook. Stop at the approval gate and tell me what it needs.", "autonomous": true, "auto_start": true}}
```

Thread the session `id` from the answer into the probe.

### 3. Run the characterization probe

Read the transcript with `workspaces__read_workspace_session_messages` and check the wiki-first shape on the `tool_call` records:

```json
{"toolset_id": "workspaces", "tool_name": "read_workspace_session_messages", "arguments": {"workspace_id": "ws-9f2c", "session_id": "sess-a1b2", "tail": true, "limit": 50}}
```

The probe passes when: the first `system__wiki_read` or `system__wiki_search` call (purpose-map, then a page) comes before the first scoped tool call of the flow; the scoped calls match the tools the page names; and there is no raw REST API exploration and no database probing in the transcript. The probe asserts the observed behavior (the documented path versus exploration), not the exact words. A live real-LLM run of this probe is CI-owned: the check above is the executable artifact, and a new agent built strictly from this recipe plus the wiki must pass it.

## Verify

The transcript shows the loop in order (index, search, read, scoped tool, approval gate stop), the session ends with the documented end state of the flow, and no record shows the agent finding a mechanism by exploration. On a fresh install the semantic rung of `system__wiki_search` is off, so the fulltext and literal modes are the reliable ones.

## Gotchas

- The prompt carries no capability text: what exists lives in the wiki, which is regenerated from live platform state at boot. A prompt that copies the wiki rots the moment the platform changes; the prompt that teaches the loop does not.
- When the wiki has no page for the flow, the correct behavior is to say the flow is not documented and ask: the probe must not reward an agent that finds the mechanism by exploring the raw API or the database. That defect class is the one this pattern exists to close.
- Do not widen the grant to make the probe pass: if the agent cannot reach a tool it should use, the fix is the core-kit door (`call_tool`), not a longer `tools` list.
- The wiki is the truth about THIS install: the live map under `this-install/` and the generated reference pages are rebuilt at every boot, so a page that names a tool is current for this install.

## Related

- [The platform wiki](knowledge/the-platform-wiki)
- [Reading this wiki](start-here/reading-this-wiki)
- [The core kit](start-here/the-core-kit)
- [Create and edit agents](agents/create-and-edit)
