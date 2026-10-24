---
title: "Read office documents"
summary: "Convert a PDF, Word, PowerPoint or HTML file in the workspace to plain text."
order: 3
tools: [system__read_doc_content]
related: [workspaces/in-session-file-tools, knowledge/documents]
---
## What it is
`system__read_doc_content` reads a file in the session's workspace and returns its text as `{text: ...}`. It is the document-shaped read: on this release it accepts UTF-8 text files (markdown, HTML source, CSV, plain text), and a file that is not UTF-8 text is refused. It needs a workspace session, and it is a user-tier tool.

## Concepts
- `path` is workspace-relative and must not escape the workspace; a missing or out-of-bounds path answers `type=not-found`.
- The file is decoded as UTF-8. A binary document (a PDF, DOCX, PPTX or an image) is refused with `type=bad-request` and the message `read_doc_content supports UTF-8 text files only; binary document conversion was removed in v2`. The conversion path that once handled PDF, Word and PowerPoint went with the ingest package in v2: a collection holds text, so converting a binary is a job for a tool the operator runs before the file reaches primer.
- An HTML file comes back as its raw markup: the tool decodes the file, it does not render the page. To read a web page as clean markdown, use `web__web_fetch` (web/search-and-fetch).
- For a plain text file, `workspace__read` is the ordinary in-session path (workspaces/in-session-file-tools); `system__read_doc_content` is the document entry point, the shape the knowledge tooling uses when a document's text has to enter a collection (knowledge/documents).
- The answer carries the whole file: there is no offset and no length argument, so a long document returns a long `text`.
- The tool is invisible to outside MCP clients: a workspace-bound tool reads `ctx.workspace_id` for its file I/O, and the stateless MCP surface has no workspace to bind (the exposure check refuses it with `needs_workspace`).

## How to
1. Get the document into the workspace: the console's file upload, `web__download` or a copy in.
2. Check that it is a UTF-8 text file; convert a binary (PDF, DOCX, PPTX) to text before it reaches primer.
3. Call `system__read_doc_content` with the workspace-relative path.
4. Use the returned text: quote it, feed it to a collection, or write it out with the workspace file tools.

## Tool calls
### Read a document
`system__read_doc_content` returns the file's text.

```json
{"toolset_id": "system", "tool_name": "read_doc_content", "arguments": {"path": "reports/q3.txt"}}
```

Reference: [`system__read_doc_content`](reference/tools/system/read_doc_content)

## Gotchas
- A binary document is refused, not converted: the `bad-request` names the removal, and retrying the same file gets the same answer.
- The refusal covers anything that is not UTF-8, not just the office formats: an encoded or compressed text file is refused too.
- A call without a workspace session is a `bad-request` (`no workspace is bound to this turn`), as is a path the workspace backend will not serve.
- Outside MCP clients cannot call the tool at all; the workspace file tools are the surface for them.

## Related
[In-session file tools](workspaces/in-session-file-tools), [documents](knowledge/documents).
