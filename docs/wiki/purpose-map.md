---
title: Purpose-mapped index
summary: What I want to do, mapped to the page that says how; the runbook set, the admin configuration flows, and the operator's own roles.
audience: admin
order: 1
related: [start-here/reading-this-wiki, runbooks, not-agent-callable]
---
The map of "what I want to do" to the page that says how. This is the first stop for any goal before acting: find the row for the goal, read the page it names, and follow the exact scoped tool calls and approval gates that page documents. A row marked "pending" names the milestone that lands the page; until it lands, the flow is not documented here, and an agent says so instead of exploring the raw REST API or probing the database for the mechanism.

## Admin configuration flows
The platform is configured through the operator (an admin tells the operator to configure; the operator walks the documented flow and makes the scoped tool calls). The access model and its roles: [start-here/roles-and-approvals](start-here/roles-and-approvals).

| I want to... | read |
|---|---|
| configure an LLM provider (register, fetch its models, make a model profile) | [runbooks/configure-provider](runbooks/configure-provider) |
| change the semantic search engine (the vector store the semantic search uses) | [runbooks/change-search-engine](runbooks/change-search-engine) |
| enable the internal collections subsystem (the platform's own wiki store: embedding provider, config, bootstrap) | [runbooks/configure-internal-collections](runbooks/configure-internal-collections) |
| change the access policy on an entity (who an agent, graph, toolset, collection, profile or provider is shared with) | [runbooks/access-policy](runbooks/access-policy) |
| change the approval policy on a tool (the gate a tool ships with, live: required or policy, who approves) | [runbooks/approval-policy](runbooks/approval-policy) |
| add a new capability (a free open-source API or MCP server, or built python code) | [runbooks/new-functionality](runbooks/new-functionality) |
| register an MCP server as a toolset and grant its tools | [runbooks/new-functionality](runbooks/new-functionality) (branch A) + [toolsets/mcp-toolsets](toolsets/mcp-toolsets) |
| write python code as a toolset and register it | [runbooks/new-functionality](runbooks/new-functionality) (branch B) + [toolsets/python-toolsets](toolsets/python-toolsets) |
| work with workspaces, sessions and agents (create, files, start a session, manage) | [workspaces/](workspaces) + [sessions/](sessions) + [agents/](agents) |
| create or manage a workspace (from a template; its files; starting a session; what deletion destroys) | [runbooks/manage-a-workspace](runbooks/manage-a-workspace) |
| create or edit an agent (profile, tool grants as scoped ids, prompt, caps; prove it runs) | [runbooks/create-an-agent](runbooks/create-an-agent) |
| create a graph and run it (validate, run in a session, read the output, clean up) | [runbooks/create-and-run-a-graph](runbooks/create-and-run-a-graph) |
| coordinate multiple agents in one workspace (the coordinator and sub-agents file pattern, the other coordination surfaces) | [collaboration/coordinator-pattern](collaboration/coordinator-pattern) + [collaboration/other-patterns](collaboration/other-patterns) |
| build a wiki-driven agent (the prompt that teaches the wiki loop, the smallest grant, the characterization probe) | [agents/wiki-driven-agent](agents/wiki-driven-agent) |
| understand what the access model allows (the roles, the grants, what "admin" means) | [start-here/roles-and-approvals](start-here/roles-and-approvals) |
| find what an entity does before configuring it | [this-install/](this-install) (the live map of this install) + `system__describe_tool` |

The pages under [providers/](providers) are concept pages (what each provider kind is); the rows above are the flows an agent walks. The generated pages under [reference/tools](reference/tools) carry the exact, validating call for every tool a page cites.

## The runbook set
One row per runbook as it lands (the authoring rule and the citation rule: [runbooks/conventions](runbooks/conventions)). Every runbook adds its row to this page in the same PR.

| goal | runbook |
|---|---|
| I want new functionality (web search first; MCP integration; builder delegation last) | [runbooks/new-functionality](runbooks/new-functionality) |
| change the access policy on an entity | [runbooks/access-policy](runbooks/access-policy) |
| change the approval policy on a tool | [runbooks/approval-policy](runbooks/approval-policy) |
| configure a provider (LLM provider + model profile) | [runbooks/configure-provider](runbooks/configure-provider) |
| change the search engine (semantic search provider) | [runbooks/change-search-engine](runbooks/change-search-engine) |
| moderate a request (plan it, create a separate workspace, launch the agents and sessions in it) | [runbooks/moderate-a-request](runbooks/moderate-a-request) |
| read the status of every visible workspace and session (which are running, which are parked and why) | [runbooks/workspace-status](runbooks/workspace-status) |
| manage a workspace (create from a template, files, sessions, deletion) | [runbooks/manage-a-workspace](runbooks/manage-a-workspace) |
| create or edit an agent and verify its answer | [runbooks/create-an-agent](runbooks/create-an-agent) |
| create and run a graph, collect its actual output | [runbooks/create-and-run-a-graph](runbooks/create-and-run-a-graph) |
| the authoring rule for a new runbook (structure, citation, index row) | [runbooks/conventions](runbooks/conventions) |

## The operator's own roles
The operator (the seeded `operator` agent) carries these roles; the pages that document them:

| role | what it is | documented at |
|---|---|---|
| identity | the operator knows who is prompting this session (the session's initiator: person id and role) and what that role may do; it is carried in the operator's context per run | [start-here/roles-and-approvals](start-here/roles-and-approvals) (the access model the identity states) |
| moderator | a normal user's request: the operator plans it (the planner), creates a separate workspace, and launches the respective agents/sessions in it | [runbooks/moderate-a-request](runbooks/moderate-a-request) |
| bookkeeper | the workspace-level status view: which workspaces, which sessions, which are parked or running | [runbooks/workspace-status](runbooks/workspace-status) |

## Related
- [start-here/reading-this-wiki](start-here/reading-this-wiki): how to read this wiki and how to call tools.
- [runbooks/](runbooks): the runbook set.
- [not-agent-callable](not-agent-callable): the surfaces no agent tool reaches (REST-only, human-only) - a runbook names these as the human step, never as an exploration target.
