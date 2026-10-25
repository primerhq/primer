---
title: "Canvas"
summary: "Show controls, media and HTML above a session's transcript, and get the user's clicks back."
order: 4
tools: [canvas__canvas_get, canvas__canvas_apply, canvas__canvas_wait]
related: [start-here/calling-tools]
---
## What it is
The canvas is a panel above the session's transcript. You draw on it with `canvas__canvas_apply` (buttons, choices, images, audio, links and your own HTML), read it back with `canvas__canvas_get` (the panel as an editable program, the values the user set, and the events since your last read), and wait for the user's next click or choice with `canvas__canvas_wait`. The canvas is the session's: anyone who may read the session may read its canvas, and what the user enters never leaves it.

## Concepts
- `canvas__canvas_apply` takes one atomic batch of Canvas Lang statements (or JSON ops). One statement per line: `id = Type(args)` creates or replaces an element; `id.prop = value` sets one property; `id += [a, b]` appends children; `del a, b` removes elements; `clear` empties the panel. If any line is wrong nothing is applied and every error names its line and the expected signature.
- The element types are `Column`, `Row`, `Card` (containers) and `Divider`, `Text`, `Button`, `Select`, `Image`, `Audio`, `Link`, `Html`; every element also takes `hidden` and `disabled`. `src` is an https URL, a `file:<workspace path>` or a small data URI.
- The user's clicks and choices reach you as text: a waking click (a Button click or an Html emit) as its own `<canvas_event>` block, and the changes you have not yet seen as a `<canvas_changes>` digest.
- A Button click wakes a `canvas__canvas_wait`; a Select change does not unless you set `wake=true` on it.
- `canvas__canvas_get` returns the Canvas Lang program you can copy, edit and resend; the user's choices are marked `# user`.
- `canvas__canvas_wait` parks the turn until the user acts; it returns the matching events with the current values, or `{timeout: true}`.
- An Html element drawn for someone else's session is inert in their console until they click Run; on the session's own session it follows the operator's strict mode.

## How to
1. Draw the panel with `canvas__canvas_apply` when the user should see or answer something richer than text.
2. Give the elements ids that say what they do; the transcript shows the element label.
3. Read the panel back with `canvas__canvas_get` before changing it, or to see what the user clicked or chose.
4. Pause with `canvas__canvas_wait` when your next step depends on a click or a choice and you have nothing else to do.
5. Act on the events: a click as its block, the unseen changes as the digest.

## Tool calls
### Draw the panel
Apply one atomic batch; if a line is wrong nothing is applied and every error is named.

```json
{"name": "canvas__canvas_apply", "arguments": {"source": "root = Column([pick, go])\npick = Select(\"Genre\", [\"Jazz\", \"Lo-fi\"], \"Jazz\")\ngo = Button(\"Play\")"}}
```

Reference: [`canvas__canvas_apply`](reference/tools/canvas/canvas_apply)

### Read the panel back
Read the whole panel as an editable program, with the values the user set and the events since your last read.

```json
{"name": "canvas__canvas_get", "arguments": {}}
```

Reference: [`canvas__canvas_get`](reference/tools/canvas/canvas_get)

### Wait for the user
Park the turn until the user acts on the panel.

```json
{"name": "canvas__canvas_wait", "arguments": {"ids": ["go"]}}
```

Reference: [`canvas__canvas_wait`](reference/tools/canvas/canvas_wait)

## Gotchas
- A Select change does not wake you unless you set `wake=true` on it.
- Nothing is applied if a line fails: the batch is all or nothing, so fix the named lines and resend.
- The canvas is the session's, not yours: it is read by anyone who may read the session, and what the user enters never leaves it.
- `canvas__canvas_wait` on an element that never sends such an event, or on an empty canvas, is refused.
- An operator's batch or a user's click is refused with a rank error if you rank below the session's creator; a token acts as its owner.

## Related
[calling tools](start-here/calling-tools), [ask the user](human-in-the-loop/ask-user), [inform the user](human-in-the-loop/inform-user).
