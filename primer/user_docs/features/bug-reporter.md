---
slug: bug-reporter
title: Bug reporter
section: features
summary: The in-UI bug-report button, the bugs/ directory layout, and the autonomous-fix loop.
---

## Why it exists

Operators using primer day to day hit UI rough edges that the
maintainers do not see. The bug reporter button captures the
moment the operator notices something off: it screenshots the
current view, captures the URL, and posts a free-text
description into the project's `bugs/` directory.

The bugs land in git, so the next developer pass can read them
in context. The same loop optionally runs autonomously: a
scheduled Claude agent reads each open bug, fixes it, and
updates the bug's status.

## The modal

The button lives in the bottom-right floating action area.
Clicking it opens the report modal:

```mockup:bug-reporter-modal
{ "pageUrl": "/console/#/sessions/sess-a1b2", "hasScreenshot": true }
```

The screenshot is auto-attached unless the operator unchecks
it. The page URL captures the route + the hash so the
maintainer can jump to the same place.

## What lands in bugs/

Each report writes a directory:

```
bugs/
  bug-2026-06-04-a1b2c3/
    meta.json        # status, timestamps, page_url
    description.md   # operator's free text
    screenshot.png   # auto-attached (optional)
```

The `meta.json` carries `status: "open"`; when the issue is
fixed the developer (or the autonomous loop) flips it to
`fixed` with a `fixed_at` timestamp and the commit sha.

## The autonomous loop

For repositories that opt in, a scheduled Claude routine runs
the bug-fix loop:

```code-tabs:bash
--- bash
# What the loop does, paraphrased:
# 1. Read every bugs/bug-*/meta.json with status == "open".
# 2. For each: read description.md and screenshot.png; investigate
#    the underlying code; write the fix; run the affected tests;
#    commit on main with a fix(...) message; update meta.json.
# 3. If the bug is ambiguous, add meta.json.blocker and move on.
uv run primer bugs run-fix-loop
```

```callout:info
The autonomous loop never pushes. Every fix lands as a commit
on main; pushing remains the operator's decision. Bugs that
the loop cannot fix surface a blocker field with a one-line
reason, so the next operator pass can decide what to do.
```

## Privacy

Screenshots can include sensitive UI state. Two safeguards:

- The operator can uncheck Attach screenshot before sending.
- The bugs/ directory is git-tracked, so the same review
  discipline that catches a secret in a regular commit catches
  a secret in a bug report.
