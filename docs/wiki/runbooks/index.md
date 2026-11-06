---
title: Runbooks
summary: Step-by-step flows an agent walks end to end; the exact scoped tool calls, the approval gates, and the verifiable end state.
audience: admin
related: [purpose-map, runbooks/conventions, reference]
---
A runbook is a dictated flow: it names the exact scoped tool calls and the approval gates on the writes, so an agent walks the documented path end to end instead of exploring the raw REST API or probing the database for the mechanism. The authoring rule a runbook follows - structure, the citation rule, the index-row rule: [runbooks/conventions](runbooks/conventions).

## The set
One row per runbook as it lands; the purpose-mapped index ([purpose-map](purpose-map)) carries the goal-to-page rows and gets a row here in the same PR as each runbook.

| runbook | the flow it dictates |
|---|---|
| [runbooks/new-functionality](runbooks/new-functionality) | "I want new functionality": web search first, user check, MCP integration, builder delegation last |
| [runbooks/access-policy](runbooks/access-policy) | change the access policy on an entity (who it is shared with) |
| [runbooks/approval-policy](runbooks/approval-policy) | change the approval policy on a tool (its gate, its approvers, its timeout) |
| [runbooks/configure-provider](runbooks/configure-provider) | configure a provider (LLM provider row, model check, model profile, bind) |
| [runbooks/configure-internal-collections](runbooks/configure-internal-collections) | enable the internal collections subsystem (the platform's own wiki store: model choice, config write, bootstrap, verify) |
| [runbooks/change-search-engine](runbooks/change-search-engine) | change the semantic search engine (the vector store provider) |

The how-to set a runbook set grows: pending rows (the moderator and bookkeeper flows) are named in the purpose-mapped index with the milestone that lands them, and appear here when they land.

## Related
- [purpose-map](purpose-map): "I want to do X" -> the page that says how.
- [reference/](reference): the exact, validating call for every tool a runbook cites.
- [not-agent-callable](not-agent-callable): the human steps a runbook names.
