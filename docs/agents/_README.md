# Agent docs (`_internal_ai_docs`)

These markdown files are the agent-facing knowledge base. At bootstrap,
the internal-collections subsystem walks this directory recursively
(`rglob("*.md")`), skips files whose name starts with `_`, and ingests
each remaining file as one Document in the reserved `_internal_ai_docs`
collection. The Document slug is the file's path relative to this
directory without the `.md` suffix (e.g. `agents`,
`cookbook/pr-reviewer-on-cron`); the Document's `path` is that slug
plus `.md`.

Agents reach these via `search::search_ai_docs(query=...)`, whose hits
carry the matched chunk text and a `document_id` equal to the slug.
The AI-docs bodies are not stored in the user-document content store,
so they are not readable through `system::get_document_content`.

Every doc starts with frontmatter (`slug`, `title`, `summary`, optional
`related`, optional `mcp_tools`) and follows the agent-doc template:
Overview, Mental model, Lifecycle, MCP tools, Workflows (with request
AND response JSON), Gotchas, Related. Recipes live under `cookbook/`.
Do not use the em-dash character anywhere.

The runtime locates this directory via `resolve_ai_docs_dir()`
(`primer/ai_docs_path.py`): `PRIMER_AI_DOCS_DIR` env override, else
`docs/agents`, else the legacy `primer/ai_docs`.
