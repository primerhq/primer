# Agent docs (the `system` collection's `docs/` subtree)

These markdown files are the agent-facing knowledge base. The platform
wiki build (`primer/knowledge/system_collection.py`) writes them into
the reserved `system` collection as the wiki's legacy layer, until the
`docs/wiki/` pages replace them: it walks this directory recursively
(`rglob("*.md")`) and writes each file as one document whose path is
`docs/` plus the file's path relative to this directory, each segment
slugified, without the `.md` suffix (e.g. `docs/agents`,
`docs/cookbook/pr-reviewer-on-cron`; this file is `docs/readme`).

Agents reach them like any other wiki page: `system::wiki_search` (or
`collections::search` with `"collection": "system"`) finds them, by
keyword and, once internal search is on, by meaning; `system::wiki_read`
(or `collections::read_document`) reads a whole doc by its path. See
`semantic-search.md`.

Every doc starts with frontmatter (`slug`, `title`, `summary`, optional
`related`, optional `mcp_tools`) and follows the agent-doc template:
Overview, Mental model, Lifecycle, MCP tools, Workflows (with request
AND response JSON), Gotchas, Related. Recipes live under `cookbook/`.
The hygiene tests (`tests/docs/test_agent_docs_hygiene.py`) skip files
whose name starts with `_`, such as this one. Do not use the em-dash
character anywhere.

The runtime locates this directory via `resolve_ai_docs_dir()`
(`primer/ai_docs_path.py`): `PRIMER_AI_DOCS_DIR` env override, else
`docs/agents`, else the legacy `primer/ai_docs`.
