---
title: "Human in the loop"
summary: "Ask a person, tell a person, show a file and draw on the canvas: every way an agent reaches the user."
order: 90
---

Four ways to reach the user: ask a question and wait, send an update, open a file in their console, or draw on the canvas above the conversation. Use [Inform the user](human-in-the-loop/inform-user) for status and [Ask the user](human-in-the-loop/ask-user) only when you need an answer.

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`canvas_api__clear_canvas`](reference/tools/canvas_api/clear_canvas) | Clear a session's canvas. | user |
| [`canvas_api__post_canvas_event`](reference/tools/canvas_api/post_canvas_event) | Record a person's click or choice on a session's canvas. | user |
| [`canvas_api__put_canvas_settings`](reference/tools/canvas_api/put_canvas_settings) | Replace the canvas settings. | admin |
| [`system__get_canvas_settings`](reference/tools/system/get_canvas_settings) | Read the canvas settings: media policy, strict mode and limits. | admin |
| [`system__update_canvas_settings`](reference/tools/system/update_canvas_settings) | Change the canvas settings; fields you leave out keep their values. | admin |
