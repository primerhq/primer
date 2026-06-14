---
slug: page-template
title: Features page template
section: _meta
summary: The canonical structure every Features page follows, plus the pre-created embeds and where the old concept prose lives.
---

## Why this exists

The docs refactor merged the old Concepts section into Features. Every
Features page now teaches one capability end to end, from the idea to
the resulting behavior. This note is the canonical template the content
tasks follow so the 27 pages read consistently. It is under `_meta/`,
so it is exempt from the lint and may show forbidden patterns by
example.

## The four-part page template

Every Features page has these sections in this order:

1. Concept - plain, user-friendly language. What is this feature, what
   problem does it solve, and the one or two ideas a reader needs before
   touching the console. Fold in the prose from the matching old
   concept page (see the mapping below) so nothing is lost.
2. Configuration - the console form and every knob explained. Walk each
   field: what it does, valid values, defaults, and when to change it.
3. Walkthrough(s) - apply the configuration step by step, UI-centric.
   Number the steps. Reference what the operator clicks and sees.
4. What happens after - the resulting behavior or outcome once the
   feature is configured. What changes, what the operator can now do.

End the page with `ref:` links to related pages.

## Visuals

- Prefer a real-component `embed:` where a fixture fits. The embeds that
  doc-foundation pre-created are listed in the next section; reuse the
  17 pre-existing embeds too.
- Use ASCII mockups where embedding is impractical (for example,
  external provider UIs like BotFather or a Slack app config screen).
- Use `mermaid` for concepts and flows.
- Use the getting-started pages (introduction, quickstart) as the
  reference for tone and density.

## Lint rules to keep in mind

- No em-dash (U+2014) anywhere. Use a single hyphen, a double hyphen,
  or reword.
- Required frontmatter keys: slug, title, section, summary.
- Body starts at h2 (`##`); the h1 title comes from the frontmatter.
- Every `ref:<slug>` resolves to a doc slug; every `embed:<id>` is in
  `primer/user_docs/_fixtures/registry.json`.

## Pre-created embeds (doc-foundation)

Each id below is registered in `registry.json` with a fixture under
`_fixtures/<id>.json` and a component mapping in
`ui/components/docs/embed-registry.jsx`. Content tasks should NOT need
to edit the registry files - just use `embed:<id>` in the page body.

- `embedding-provider` -> ProvidersPage (kind=embedding)
- `ssp` -> SSPListPage
- `cross-encoder-provider` -> ProvidersPage (kind=rerank)
- `web-search` -> WebSearchPage
- `workspace-provider-create` -> WorkspaceProvidersPage
- `channel-provider-create` -> ChannelProvidersPage
- `harness` -> HarnessesPage
- `mcp-exposure` -> MC_McpPage
- `approvals` -> ApprovalsPage
- `toolsets` -> ToolsetsPage
- `collection-create` -> CollectionsPage

## Where the old concept prose lives

doc-foundation did NOT delete the old concept pages. They remain on
disk under `primer/user_docs/concepts/` (reachable by slug, hidden from
the left nav because they are no longer in the manifest). Content tasks
should fold the relevant prose into the Concept section of the matching
Features page, then the concept file can be removed by that task.

Mapping (old concept file -> target Features page):

- `concepts/what-is-an-agent.md` -> `features/agents.md`
- `concepts/sessions.md` (slug sessions-concept) -> `features/sessions.md`
- `concepts/chats.md` (slug chats-concept) -> `features/chats.md`
- `concepts/workspaces.md` (slug workspaces-concept) ->
  `features/workspace-providers.md` and `features/workspace-templates.md`
- `concepts/toolsets-and-tools.md` -> `features/toolsets-system.md`
- `concepts/triggers-and-subscriptions.md` -> `features/triggers.md`
  and `features/yielding-tools.md`
- `concepts/tool-approval.md` (slug tool-approval-concept) ->
  `features/toolsets-approvals.md`
- `concepts/yielding-and-claims.md` -> `features/yielding-tools.md`
  and `features/workers.md`
- `concepts/troubleshooting.md` was MOVED to
  `reference/troubleshooting.md` (section reference).

## Orphaned old Features pages

The previous Features section had pages whose slugs are not in the new
list. They remain on disk but are out of the manifest. Content tasks
should reuse their prose where it maps, then remove the stale file:

- `features/agents-advanced.md` -> fold into `features/agents.md`
- `features/auth-and-tokens.md` -> fold into `features/mcp-server.md`
- `features/knowledge-collections.md` and
  `features/knowledge-documents.md` -> fold into
  `features/collections-and-documents.md`
- `features/semantic-search.md` -> fold into
  `features/semantic-search-providers.md`
- `features/tool-approval.md` -> fold into
  `features/toolsets-approvals.md`
- `features/workers-and-health.md` -> fold into `features/workers.md`
- `features/workspaces.md` -> fold into `features/workspace-providers.md`
