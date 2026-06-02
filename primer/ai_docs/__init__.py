"""Agent-facing platform documentation, shipped with the package.

This directory holds the markdown source files for the
``_internal_ai_docs`` reserved collection. The internal-collections
subsystem walks ``*.md`` files here at bootstrap, chunks each via the
Docling-backed :class:`primer.ingest.DocumentIngester`, embeds the
chunks, and inserts records into the vector store.

Files prefixed with an underscore (e.g. ``_README.md``) are skipped at
ingest so internal notes don't pollute search results.

The matching MCP tool for retrieval is ``search::search_ai_docs`` (see
:mod:`primer.toolset.search`).

See ``AGENTS.md`` at the repo root for the operator-facing entry point.
"""
