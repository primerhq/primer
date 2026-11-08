---
title: "Built-in toolsets"
summary: "The toolsets that ship with primer, what each one is for and which ones need a workspace session."
order: 1
related: [toolsets/list-and-describe-tools, start-here/calling-tools]
---
## What it is
A built-in toolset is a group of tools that ships inside primer. Two kinds ship: the ten curated toolsets named in the table below, and the per-resource toolsets the app compiles from the reviewed REST routes at startup. You reach every one of them through `system__call_tool`, or call a tool directly when your agent's `tools` list grants its scoped id (`<toolset>__<tool>`).

## Concepts
The ten curated toolsets:

| Toolset | What it is for | Pages | Needs a workspace session | Pauses the turn |
|---|---|---|---|---|
| `system` | The platform's own rows and services: agents, graphs, collections and documents, providers, channels, approval policies, stored toolsets and the wiki, plus the doors `system__call_tool`, `system__describe_tool` and `system__list_toolset_tools`. Many of its tools need an admin. | [list and describe tools](toolsets/list-and-describe-tools), [agents](agents/create-and-edit), [collections](knowledge/collections), [LLM providers](providers/llm), [channels](channels/channels) | `system__read_doc_content`, `system__wait_for_event` | `system__ask_user`, `system__wait_for_event`, and `system__call_tool` when its inner tool pauses or is gated |
| `workspaces` | Workspaces, templates, workspace providers, sessions, and messages between sessions. | [create and delete](workspaces/create-and-delete), [start a session](sessions/start-a-session), [messaging other sessions](sessions/messaging-other-sessions) | `workspaces__list_my_sessions`, `workspaces__message_session` | none |
| `workspace_ext` | The waits of a running session: sleep, watch files, run a graph, wait for a trigger or a channel event. | [sleep and watch files](utilities/sleep-and-watch-files), [waiting for events](triggers/waiting-for-events), [run a graph](graphs/run-a-graph) | every tool | every tool |
| `trigger` | Triggers, their subscriptions, and firing a trigger now. | [triggers](triggers/triggers), [subscriptions](triggers/subscriptions), [fire now](triggers/fire-now) | none | none |
| `harness` | Harnesses: register, fetch, install, sync and uninstall a git bundle of agents, graphs, triggers and toolsets. Most of its tools need an admin. | [what a harness is](harnesses/what-a-harness-is), [install and sync](harnesses/install-and-sync) | none | none |
| `collections` | Documents addressed by path: list collections, walk a tree, read, search, write, move and delete. | [navigation](knowledge/navigation), [search](knowledge/search), [documents](knowledge/documents) | none | none |
| `web` | Web search, page fetch, HTTP requests and downloads. | [search and fetch](web/search-and-fetch), [HTTP and downloads](web/http-and-downloads) | `web__download` | none |
| `misc` | The time, UUIDs, hashes, arithmetic and `misc__inform_user`. | [time, ids, hashes and math](utilities/time-ids-hashes-math), [inform the user](human-in-the-loop/inform-user) | none | none |
| `crud` | Create and update agents, graphs, triggers and Python toolsets. Every tool in it is approval-gated by default. | [agents](agents/create-and-edit), [graphs](graphs/create-and-edit), [triggers](triggers/triggers), [Python toolsets](toolsets/python-toolsets) | none | none |
| `canvas` | The session canvas toolset, on by default now that the canvas panel ships (an operator can switch it off with the setting `canvas.agent_tools_enabled`). | [the canvas](human-in-the-loop/canvas) | every tool | `canvas__canvas_wait` |

The per-resource toolsets are the other half of the built-in surface. The app compiles one of them per API resource from the reviewed REST routes it declares, at startup. Each carries the resource's id, its tools are the resource's operations named `<resource>__<operation>`, and it joins the same catalogue, allowlist, approval and exposure rules as the ten curated toolsets above, so `system__call_tool` reaches it the same way (ask `system__list_toolset_tools` for its tools, or `system__describe_tool` for one tool's arguments). Every generated toolset id, one per resource:

`admin`, `admin_users`, `agent_memory`, `agents`, `artifact_storage_providers`, `audio`, `auth`, `canvas_api`, `capabilities`, `channel_providers`, `channels`, `collections_api`, `cross_encoder_providers`, `documents`, `embedding_providers`, `event_subscriptions`, `events`, `external_tools`, `graphs`, `harnesses`, `health`, `internal_collections`, `llm_providers`, `mcp`, `model_profiles`, `oidc_providers`, `providers`, `semantic_search_providers`, `services`, `sessions`, `setup`, `speech`, `sso_settings`, `ssp`, `stt_providers`, `svc`, `tool_approval_policies`, `tools`, `toolset_oauth`, `toolsets`, `triggers`, `tts_providers`, `users`, `web_fetch`, `web_fetch_providers`, `web_search`, `web_search_providers`, `webhooks`, `workers`, `workspace_access`, `workspace_events`, `workspace_files`, `workspace_log`, `workspace_providers`, `workspace_sessions`, `workspace_templates`, `workspace_yields`, `workspaces_api`, `yields`

Three more ids are scopes, not toolsets. You cannot reach them through `system__call_tool`:
- `workspace`: the seven session file tools (`workspace__ls` to `workspace__exec`). They are always on in a workspace session and you call them directly. See [in-session file tools](workspaces/in-session-file-tools).
- `client`: `client__open_file`, the console's per-turn grant, also called directly. See [open a file](human-in-the-loop/open-file).
- `external`: the tools an API caller hands an agent with one invocation, when the agent sets `allow_external_tools`. See [tools and grants](agents/tools-and-grants).

Stored toolsets sit next to the built-in ones: an [MCP toolset](toolsets/mcp-toolsets) is a remote or local MCP server, and a [Python toolset](toolsets/python-toolsets) is one module of Python functions. Each is a `Toolset` row with its own id, and its tools are named `<toolset id>__<tool>` like any other. A stored toolset cannot take a built-in id or a scope id: the create answers `conflict`.

"Needs a workspace session" means the tool reads the session's workspace, or parks the session. Outside a session (an outside MCP client, for one) it is not offered.

## How to
1. Pick the toolset from the table, then read its page for the call order.
2. Read the exact call on the tool's reference page, `reference/tools/<toolset>/<tool>`, or ask `system__describe_tool`.
3. Call the tool through `system__call_tool`, or directly when your agent grants it. See [list and describe tools](toolsets/list-and-describe-tools).

## Gotchas
- `system__list_toolsets` and `system__get_toolset` read stored rows only. A built-in id answers `not-found` there. To see what a built-in toolset offers, use `system__list_toolset_tools`.
- The `harness` tools carry the bare names (`list`, `install`, ...), scoped like every other toolset to `harness__list`, `harness__install` and the rest. An agent's `tools` list grants them like any other tool; `system__call_tool` reaches them with `"toolset_id": "harness"` and `"tool_name": "list"`.
- The `workspace_ext` tools exist only inside a workspace session. A grant of them does nothing anywhere else.
- A tool that pauses the turn parks the session until its answer or its deadline arrives. Pausing tools are never offered to outside MCP clients.
- The retired ids `_system`, `_workspaces` and `_misc` still resolve to `system`, `workspaces` and `misc`, so an old agent row keeps working. Write the current ids.
- In the console, the `collections` card shows "Internal search: off" until an admin turns internal search on. Until then, a search of the `system` collection matches keywords only (see [search](knowledge/search)).

## Related
[List and describe tools](toolsets/list-and-describe-tools), [calling tools](start-here/calling-tools), [MCP toolsets](toolsets/mcp-toolsets), [Python toolsets](toolsets/python-toolsets).
