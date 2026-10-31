# Canvas

The agent canvas of a session: a panel above the session's transcript where the agent draws controls, media and
HTML and the user touches them. The subsystem is `primer/canvas`. Its core is pure (no I/O, no
storage, no clock); around it sit the parts that touch storage, the platform event log and the
session's message log.

## Purpose

The canvas gives the agent a richer way to reach the user than text: it draws buttons, choices,
images, audio, links and its own HTML, and it reads back what the user did. The agent talks to it
through three tools: `canvas_get` (read the panel as an editable program, the user's values and
the events since the agent's last read), `canvas_apply` (change the panel in one atomic batch) and
`canvas_wait` (pause the turn until the user acts). A user's click or choice reaches the agent as
text the agent can act on, by the state of the session. The panel is the session's: any user who
may read the session may read its canvas, and what the user entered stays in the session.

## Conceptual model

**Canvas Lang.** The agent draws with one statement per line (only a triple-quoted string may span
lines). The statements are:

```
id = Type(args)    create or replace the element id
id.prop = value    set one property (null removes it)
id += [ids]        append children to a container
del id, id         remove elements and their subtrees
clear              remove everything but an empty root
# comment          a comment, outside strings
```

Values are double- or single-quoted strings with JSON escapes, triple-quoted strings taken
verbatim, numbers, `true`, `false`, `null`, lists (a bare id inside a list is a reference to an
element) and JSON-style objects. `...` as a whole argument or property value is "keep the current
value". A batch is applied all or nothing: if any line is wrong nothing is applied and every
error names its line and the expected signature, so the agent fixes the lines and resends.

**The element table.** `primer/canvas/catalog.py` names every element and its arguments.
Containers (`Column`, `Row`, `Card`) hold `children`; leaves are `Divider`, `Text`, `Button`,
`Select`, `Image`, `Audio`, `Link` and `Html`. Every element also takes `hidden` and `disabled`.
The element that sends an event, and which of its events wake the agent, is fixed by the table:
`Button` sends a `click` (waking, overridable by its `wake` property), `Select` a `change`
(silent unless `wake=true`), `Audio` `play` / `pause` / `ended` (silent), `Link` an `open`
(silent) and `Html` an `emit` (waking) and a `set_value` (silent). The kinds a canvas can carry are
`click`, `change`, `emit`, `set_value`, `play`, `pause`, `ended` and `open`.

**The decompiled form.** `canvas_get` returns the canvas as a Canvas Lang program that recreates
it: the elements in pre-order from the root, the user's choices as ordinary `id.value = ...` lines
marked `# user`, long Html bodies elided to `...` unless `full=true` is asked for, and the events
since the agent's last read as comments. The agent copies a line, edits it and sends it back.
Required and non-boolean optional arguments are positional until the first one left out; booleans
and everything after a gap are `name=value`; defaults are omitted (the stored form never holds
them).

**The state (Decisions 1 to 3).** A canvas is one row per session (`SessionCanvas`, table
`sessioncanvas`, id is the session id). It holds the flat, id-keyed `elements`, the live
`values` of the controls and who set each (`value_set_by`), a ring of the latest `events`, and
the counters that fence every write.

- `rev` counts content changes: a batch (agent or operator), a clear and a value-changing event
  (`change`, `set_value`) bump it. A click, a play, a pause, an end, a link open or an emit bumps
  `event_seq` instead. The report's "rev is the record seq" is dropped: a record's seq cannot be
  allocated outside a turn's writer (Decision 2), so `rev` and the record seq are independent.
- `value_seq` moves with every event that writes `values` (a value change, and an audio event,
  which sets the element's play state without moving `rev`). A batch is fenced on `rev` and
  `value_seq` only: it writes `elements`, `values` and `value_set_by` and nothing an event writes
  besides, so an event that leaves the values alone never fails an agent's batch, while one that
  set a value since the batch read the row makes it read again and no value is lost.
- The row is the source of truth: nothing is rebuilt from the session log. A rewind, a compaction,
  a restart or a binding switch does not touch the canvas. (The report's "rebuild from the log" is
  dropped for this reason.) Deleting a session deletes its row, best effort.

**Records and events.** A batch (an agent's `canvas_apply`, or an operator's) writes a
`canvas_op` record to the session's message log; a user's interaction writes a `canvas_event`
record. The rule of where a record lands (Decision 2) depends on whether a turn is running:

- Inside a turn (an agent applies a batch from a tool call) the turn's own writer owns the seqs;
  the record goes through it (reached through the delegation recorder the dispatch publishes on a
  contextvar), so it takes the next seq of the turn's counter and lands in order.
- Outside a turn (an operator's batch, a clear, a user's click) the record reserves the next seq
  on the session row, guarded on the turn not running (`idle` or `claimable`), and is appended at
  that seq.
- While a turn is running and the caller is not the turn, nothing is written. The batch or the
  event is still in the canvas row and in the platform event log, and a silent event reaches the
  agent in the next digest, so only the transcript line is skipped.

A record is advisory: a canvas that has been written never fails because its transcript line could
not be, so the writer logs a warning and returns rather than raise. A platform event is published
for each write: `canvas.updated` for a batch, `canvas.event` for an interaction.

**Delivery by session state.** When a user interacts, the canvas records the event (the canvas
row, the `canvas.event` platform event, the frame on the session's delta channel) and then hands it
to the agent, by the state of the session, read fresh:

| the session is | the outcome |
|---|---|
| ended, or gone | `recorded` (the route refuses an ended session before it records) |
| parked on a `canvas_wait` the event matches (or a wait the flip made resumable with this event as its answer) | `wait` (the event dispatcher resumes the wait; here only the record is written) |
| a silent event (a Select change, an audio event, a link open, an Html `set_value`) | `recorded` (skipped while a turn runs); it reaches the agent in the next digest, or in `canvas_get` |
| a waking event the agent has already read (the canvas was read again) | `recorded`, and nothing is written; it was delivered |
| a waking event while a turn is open, or while the session is parked on anything else | `queued`, as a steer whose text is the `<canvas_event>` block; the park is untouched, so a click never answers a gate |
| else | `woken`, the block as the session's next instruction (a click is a person) |

**canvas_wait.** `canvas_wait` parks the turn until the user acts, and returns the matching events
with the current values, or `{timeout: true}`. An event matches when its element is in `ids` (or
`ids` is empty) and, when `kinds` is given, its kind is in `kinds`; without `kinds` only waking
events match. The same rule builds the subscription filter and the Python predicate, and a test
pins that they agree. Default timeout 600 s, at most 86400. A wait nothing could end (an element
that never sends such an event, or an empty canvas) is refused.

**The digest.** `wake_session` adds a digest to any instruction it writes (a typed message, a
realized steer, a canvas click): `<canvas_changes>` with the unseen events (at most 10 lines),
excluding the event being delivered. A click reaches the agent once, as its own `<canvas_event>`
block; the silent events it has not seen ride the digest.

## Architecture patterns implemented

The canvas writes follow the storage pattern: every write to the row is one field-scoped
`patch_if` fenced on the counters that were read (the first write of a session is a `create`).
`CanvasService` is the single writer; an event is fenced on `rev` and `event_seq`, a batch on
`rev` and `value_seq`. A writer that lost the race reads the fresh row and decides again, up to
five times, then raises a conflict, so an API pod recording a click and a worker applying an
agent's batch both land, `rev` and `event_seq` only ever increase, and nothing writes the whole
row. The element table, the language and the decompiler are plain data and plain functions with
no I/O, so they are tested directly (see Testing patterns). The settings are one row read and
written through the storage abstraction, the same way the rest of the platform keeps its single
rows.

## Code layout

- `primer/canvas/lang.py` - Canvas Lang syntax: the splitter, the recursive-descent parser, the
  statements and the canonical literals.
- `primer/canvas/catalog.py` - the element table: the types, their arguments and the event kinds.
- `primer/canvas/state.py` - the stored form (`CanvasState`) and the atomic batch applier.
- `primer/canvas/ops.py` - the JSON ops form of a batch.
- `primer/canvas/decompile.py` - the decompiled program `canvas_get` returns and the event lines.
- `primer/canvas/html_check.py` - the inline-script syntax check and the CDN rule.
- `primer/canvas/settings.py` - reading and writing the single settings row.
- `primer/canvas/records.py` - writing a canvas record into the session's message log.
- `primer/canvas/service.py` - `CanvasService`: apply a batch, record an interaction, keep the
  row fenced, and make the best-effort announcements.
- `primer/canvas/wait.py` - the `canvas_wait` matching rule (the subscription filter's predicate).
- `primer/canvas/delivery.py` - delivering a user's interaction to the agent by session state.
- `primer/canvas/blocks.py` - the `<canvas_event>` and `<canvas_changes>` text the agent reads.
- `primer/canvas/frame.py` - the frame shell, its CSP and its headers.
- `primer/canvas/files.py` - the capability URLs for a workspace file a canvas shows.
- `primer/model/canvas.py` - `SessionCanvas`, `CanvasEvent` and `CanvasSettings`, and the
  constants.
- `primer/api/routers/canvas.py` - the HTTP surface (the frame, the files, the console routes, the
  admin settings).
- `primer/toolset/canvas.py` - the `canvas` toolset and its three tools.
- `ui/canvas/` - the frame: the template, the CSS, the trusted renderer and the Html bootstrap.

## Data model

`SessionCanvas` (id is the session id) holds the canvas of one session:

- `elements`: the flat, id-keyed map of the elements (the root is always a Column).
- `values`: the live value of each control; `value_set_by` records who set each (`agent` or
  `user`).
- `events`: a ring of at most `max_events` `CanvasEvent`s; `event_seq` numbers them.
- `seen_event_seq`: how far the agent has read the ring in order; `seen_ahead`: the events past
  that point it has already read (a `canvas_wait` takes only the events it matches, so an earlier
  one it did not match stays unread); `seen_version` fences writes of the two.
- `value_seq`: moves with every event that writes `values`.
- `wake_times` and `silent_times`: the rings the waking-events-per-minute and the
  silent-events-per-minute limits read.
- `client_events`: the last client retry keys, each with the event's seq, so a retried POST is
  recognised however short the event ring is.

`CanvasEvent` is one thing a user did on a canvas, as the server validated and numbered it: its
`element`, `type`, `kind` and `source` (`control` or `html:<element id>`), the `by` (the caller's
username), whether it `wake`s, its `value` and `name`/`data`/`position`/`label`, and a client
retry key. Its `public()` form is the event as the platform event and the record carry it.

`CanvasSettings` is the single operator row (`_canvas_settings`): `agent_tools_enabled`, the media
policy (`any-https`, `allowlist` or `none`), the media allowlist, `strict_mode`, and the five
limits (`max_elements`, `max_html_bytes`, `max_canvas_bytes`, `max_events`,
`max_waking_events_per_minute`). `agent_tools_enabled` defaults to true (since S8 shipped the
canvas panel); while off, the canvas toolset is hidden from the tool catalogue and from
`call_tool`, so an agent cannot draw or read the canvas, and the admin settings dialog edits the
row. The defaults are 200 elements, 65536 bytes of one Html body, 524288 bytes of a whole canvas,
100 retained events and 10 waking events per minute. The canvas GET embeds the effective media
policy and strict mode for the renderer.

## Lifecycle

`CanvasService` owns the writes. `apply` takes the stored form and a batch and either returns the
new form or refuses the whole batch with an error for every bad line; it never mutates its input
and never touches `rev`. `record_event` validates a user's interaction, numbers it and writes it
to the row. After a write, best effort (a canvas that has been written never fails because its
announcement could not be made), the service makes three things:

- the transcript record (`canvas_op` or `canvas_event`), under the record rule above;
- the platform event (`canvas.updated` for a batch, `canvas.event` for an interaction);
- the `canvas` frame on the session's delta channel, so a console refetches the snapshot.

A refused batch or a refused event writes nothing and announces nothing. The workspace an
announcement is made in is the one the canvas row names (never the caller's): the record is
appended through the workspace's own log, so a caller's idea of the workspace must not move where
a session's canvas speaks.

## Persistence

The canvas is one row per session, written only ever by a field-scoped `patch_if` fenced on the
counters that were read; the settings are one row. A canvas record is appended to the session's
`messages.jsonl`, which has one writer at a time per session (see Records and events). A canvas
that has been written must not fail because its transcript line could not be, so the record
writer never raises. Deleting a session deletes its canvas row, best effort.

## Public surfaces

**The tools.** The `canvas` toolset exposes three tools, role `user` and
`requires_workspace` (they act on the calling session), so they are dropped outside workspace
sessions and are not MCP-exposable. `canvas_get` reads the panel as a Canvas Lang program (the
user's values, the events since the last read; `ids` limits it, `full` writes Html bodies out,
`format=json` returns the stored form). `canvas_apply` changes the panel in one atomic batch of
Canvas Lang `source` or JSON `ops`, optionally against `base_rev` (refused, and the user's changes
since then listed, when the canvas is at another rev); it is not interruptible, so a cancel
between the snapshot write and the record cannot leave a canvas the transcript does not show.
`canvas_wait` parks the turn for the user's next act. The admin's settings are a separate pair of
system tools (`system__get_canvas_settings`, `system__update_canvas_settings`), not in the
`canvas` toolset.

**The REST.** The console's surface is under `/v1` for any signed-in user:
`GET .../sessions/{session_id}/canvas` reads the canvas (the elements, the `values` and the
newest events with what the users entered); `GET .../canvas?format=lang` reads it as a Canvas Lang
program; `POST .../canvas/ops` writes an operator's batch; `DELETE .../canvas` clears it; and
`POST .../canvas/events` records a person's click or choice and hands it to the agent. The read
needs read on the session's workspace and each write needs full on it, like a steer (a token acts as
its owner); a click that wakes the agent runs that turn as the clicker. The admin's settings are
`GET` and `PUT /v1/canvas_settings`.

**The frame.** The console never puts canvas content into its own DOM: it mounts an iframe with
`sandbox="allow-scripts allow-forms"`, a static page served outside `/v1` with its own policy
(`GET /canvas/frame`). The CSP header is the canvas's only script control: it sandboxes the frame
without its origin (an opaque origin, no cookies), allows scripts only inline and from the four
CDNs, and connects nowhere. The header ends `form-action 'none'; base-uri 'none'; object-src
'none'; frame-ancestors 'self'` (matching `X-Frame-Options: SAMEORIGIN`). For a plain-http request
the request's own origin is added to `img-src` and `media-src`, so a workspace file's capability
URL loads on an http install. Every Html element is a `srcdoc` frame of this page, so the header
binds every Html frame too; each Html frame may only add its own stricter media policy (a `<meta>`
CSP that names only `img-src`, `media-src` and `font-src`, each a subset of the header's). On top
of that script-source control the renderer runs an emit gate (audit G8-05): it forwards an Html
element's `emit`, `copy` and `open_link` only while that frame's `navigator.userActivation.isActive`
holds (a click or key press inside the frame, the element's frame the document's active element,
the page visible), at most three calls per owning frame per activation (one continuous `isActive`
window, a few seconds after the last input, not one click), and a dropped call is answered
`{type: "error"}` on the element's port and counted to the console; a browser without
`navigator.userActivation` forwards none.

**The media policy and strict mode.** The media policy is `any-https` (the default: agents can
already reach the web through tools), `allowlist` (only the listed hosts, click to load) or `none`.
The renderer applies the policy to Image and Audio (load, a click-to-load placeholder, blocked).
With `strict_mode` on, an Html element stays inert until the user clicks Run; off, its scripts run
at once. Whichever the viewer sees, an Html element drawn by someone other than the session's
creator is rendered inert in that viewer's console (the creator's own view follows
`strict_mode`).

**Capability URLs.** A workspace file a canvas shows is served by a capability URL,
`GET /canvas/sessions/{session_id}/files/{token}`, outside `/v1` and without cookies (the frame is
cross-site, so a file URL is its own credential). The token is a signed, timed serializer over the
session and the path, valid 900 s; the GET canvas route mints one per referenced file. The route
serves only a file some Image or Audio element references, with an allowlisted media type,
`Cross-Origin-Resource-Policy: cross-origin` and a sandbox; every refusal is the same 404.

**The bridge.** The renderer frame and the console talk over a `MessageChannel`: the renderer
posts its ready handshake and the state it reads, the console posts the snapshot it serves.
`parent.postMessage` from the frame is open, so the console checks `event.source` (the frame's
window) and the message shape before acting on it, and accepts nothing from the canvas tree on
`window` after the handshake (only on its MessagePort).

## Internal contracts

**What the user entered never leaves the session.** A click's `value` and an Html emit's `data`
are in the text the session's own agent reads (the `<canvas_event>` block and the
`<canvas_changes>` digest), and nowhere else. Every structured copy is the event without those two
keys, removed and not masked: the queued steer's `attribution` and the `user_input`'s
`payload.canvas_event` (served to any user), the digest's payload, and the `canvas_event` record.
Rendering one would re-leak the user's input to every session viewer.

**Read marks.** `wake_session` marks read the events it hands the agent and no other: the click it
delivers (its seq rides `payload.canvas_event`) and the digest's lines. The digest lists only the
silent events, so a click reaches the agent once. A canvas failure never fails a wake: the session
core reaches the delivery module lazily, and a canvas error is caught.

**The script check.** The Html inline-script check is an early warning for the agent, never a
gate (the CSP header is the control). Classic inline scripts are compiled with V8 (never run,
never wrapped, so a crafted script cannot escape into the checker); module scripts are not checked;
a `src` must be https on one of the four CDNs.

## Testing patterns

The pure core is tested directly over plain data: the language (the statements, the values, the
exact error messages), the element table (every argument, the event kinds), the batch (a refused
line refuses the whole batch; a value set to what it already is changes nothing), the decompiler
(the program recreates the canvas; the event lines), and the script check. `CanvasService` is
tested against a real store for the fencing: a lost race reads the fresh row and retries; a batch
and an event that race both land; no value is lost. The record rule is pinned against the session
writer set (no canvas record while a turn runs; the sink path when the delegation is set). The
delivery outcomes are pinned against the session's state (a click never answers a gate; an ended
session is refused before it records). The frame's CSP and its subset for Html are pinned as
strings; the frame files are scanned so none makes a frame from a `data:` or `blob:` URL. The
transcript chips and the settings dialog are pinned in the UI tests. The e2e and ui_e2e journeys
(the agent draws and the user's clicks reach it; the panel's isolation, the Html click gate and
the phone sheet) are lead-scheduled.

## Historical decisions

The research report's options, and what was dropped:

- **"rev is the record seq" is dropped** (Decision 2): a record's seq cannot be allocated outside
  a turn's writer, so `rev` and the record seq are independent counters.
- **"rebuild from the log" is dropped** (Decision 3): the snapshot row is the source of truth; a
  rewind, compaction, restart or binding switch does not touch the canvas.
- **The media policy's default was dropped from the report's allowlist** to `any-https` (the
  operator can tighten it to `allowlist` or `none`).

Accepted residuals (documented, accepted):

- **WebRTC egress:** the Html frame's CSP carries `webrtc 'block'` and the bootstrap removes the
  RTC constructors, but browsers that ignore the directive can still open a WebRTC channel;
  Chromium logs the unrecognized directive as a console error (a ui_e2e journey that asserts no
  console errors on a page with an Html element passes that one line in its ignore patterns).
- **An Html frame navigating itself:** an Html frame may navigate its own srcdoc within its
  sandbox; it cannot leave it (opaque origin, no `connect-src`), but it can change what is shown.
- **Media URLs the agent controls:** an Image or Audio `src` the agent writes is its to choose
  (within the media policy); the console does not vet each URL beyond the policy and the CDN rule
  for scripts.
- **First-frame-owns-activation:** within one canvas, an Html element that steals the focus by
  script during another element's real activation can own that activation if its message arrives
  first; both frames are the same canvas's agent code, so this grants no gesture the agent could
  not already get on that click.
