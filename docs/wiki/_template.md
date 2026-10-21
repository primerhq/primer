---
title: Feature page template # required, string: the page's title; the build renders it as the "# " heading
summary: Template for a new wiki page; copy it and fill every section. # required, one line, at most 160 characters
audience: user # optional: user or admin (the default is user)
tools: [misc__get_datetime] # optional: the scoped tool ids this page documents; each must exist
related: [start-here/reading-this-wiki] # optional: wiki paths this page points to; each must resolve
order: 0 # optional: integer; siblings sort by order, then by title
---
Copy this file for a new feature page. One .md file per page under docs/wiki; the page path is the file path without .md, and every folder keeps its index.md.

## What it is
One or two sentences: what the feature is and who it is for.

## Concepts
The names and relationships a reader must have before the steps make sense.

## How to
Numbered steps for the common task.

## Tool calls
One json block per call, with concrete values that validate against the live schemas. A tool reached through call_tool:

```json
{"toolset_id": "misc", "tool_name": "get_datetime", "arguments": {}}
```

## Gotchas
The failures that surprise a first caller, and what to do instead.

## Related
Links to the neighbouring pages.
