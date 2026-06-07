---
slug: bug-reporter
title: Bug reporter
section: features
summary: Use the floating bug-report button to capture a screenshot and description and file it under bugs/ for the operator or maintainer to address.
---

## Overview

The bug reporter is a floating button fixed to the bottom-left of every console page. Clicking it captures a screenshot of the current view, records the page URL, and opens a modal where you describe what went wrong. The report is saved to the `bugs/` directory on disk for the maintainer to read.

## Filing a report

1. Navigate to the page where you noticed the problem. The screenshot is taken of whatever is currently visible, so stay on the relevant screen before clicking.
2. Click the red circular **alert** button in the bottom-left corner of the console. The button shows a wait cursor while the screenshot is being captured.
3. The **Report a bug** modal opens. If the screenshot was captured successfully, a preview image appears at the top of the modal.
4. Type a description of the problem in the text area. Describe what you expected to happen and what actually happened. The description field is required.
5. Click **Submit**. A success toast confirms the report was saved to disk.

The modal closes and the description field resets. You can file another report immediately if needed.

```callout:warning
Screenshots are captured at 1x scale regardless of display pixel ratio to stay within the server's upload size limit. On high-DPI screens the preview may look lower resolution than the actual display -- this is expected. If the screenshot library cannot capture the page (for example, due to CORS restrictions on embedded iframes), the modal still opens and you can submit a text-only report.
```

## What gets saved

Each report is written to a folder under `bugs/` on the server:

```
bugs/
  bug-<timestamp>-<id>/
    meta.json        # status, page_url, viewport, captured_at
    description.md   # the text you entered
    screenshot.png   # attached when capture succeeded
```

The `meta.json` carries `"status": "open"`. A maintainer or automated process marks it `"fixed"` with a `fixed_at` timestamp and commit SHA once the underlying issue is resolved.

## Privacy

Screenshots can capture sensitive console state. Two things to be aware of:

- If the current view contains information you do not want in the report, cancel the modal and navigate away before filing.
- The `bugs/` directory is git-tracked. The same review that catches secrets in regular commits also catches anything in a bug report.
