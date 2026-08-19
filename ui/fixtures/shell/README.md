# Fresh shell: designer input package

One surface. The console IS the studio: a workspace-scoped, IDE-shaped
shell that lands on the default workspace's most recent session. Everything
else is an overlay or a palette verb. There is no page router.

This directory is the stub data. Every file here is the shape the real API
returns; nothing in the shell may need a field that is absent here.

## Docs (center tabs)

Four doc kinds, addressed by wire id:

- `session` : transcript + composer + binding chip + Trace tab + inline
  gates + voice affordances. Fixtures: `session-detail.json`,
  `session-list.json`, `tap-frames.json`, `turn-timeline.json`.
- `file` : workspace file viewer/editor. Fixtures: `files-tree.json`,
  `file-read.json`.
- `diff` : one commit's changes, per-file patches. Fixtures:
  `commit-log.json`, `commit-diff.json`.
- `wiki` : a collection document addressed by its slug path. Fixture:
  `wiki-document.json`.

## Overlays

Each is a self-contained management surface, opened over the shell and
addressable in the URL. Names are the registry keys:

`providers`, `collections`, `agents`, `graphs`, `triggers`, `toolsets`,
`tools`, `workers`, `approvals`, `admin`, `harnesses`, `services`,
`channels`, `workspaces`.

Thirteen re-host an existing component with no chrome. `admin` is the only
overlay designed from scratch: ONE search-first surface holding users, SSO,
API tokens and MCP settings, common settings at level one, "Advanced"
collapsed, admin sections role-gated, every setting palette-addressable.

Fixtures: `pending-yields.json`, `approval-records.json`,
`workers-stats.json`, `capabilities.json`, `auth-status.json`.

## URL-as-state

Canonical form, hosted inside the console's hash fragment:

    #/w/{wid}?doc=<kind>:<ref>&overlay=<name>[:<section>[:<id>]]#<anchor>

Examples: `#/w/ws-3f8a?doc=session:sess-4f1a#turn-42`,
`#/w/ws-3f8a?doc=file:src/api.ts#L10-L30`,
`#/w/ws-3f8a?overlay=providers:tts:pv-1`.

Verb navigation writes history. Transient UI (palette open, toasts) stays
out of the URL. Refresh and pasted links restore exactly.

## Binding UX rules (contract, not suggestions)

- **Always-on status verb**: whenever a run is active, verb + object +
  elapsed ("running: grep src/ - 12s") with the interrupt affordance beside
  it, rendered identically in the composer status strip, the rail row chip
  and the tab label. Mounted immediately on send. Never a bare spinner.
- **Two-phase turn rendering**: expanded while running (tool chips, short
  output previews, "see more"); collapsed to named sections plus the final
  message on completion.
- **Tool chips** speak plain language ("searched 4 files"). Writes are
  prominent and open the diff or file tab on click; reads are grouped and
  subdued. Raw arguments never appear inline; the Trace tab holds the
  exhaustive record.
- **Scroll anchoring**: auto-follow only within about 100px of the bottom.
  Scrolling up freezes the viewport and shows "Jump to latest - N new
  turns". Agent-driven opens obey the same law: background tab, badge, and
  a narration line; never focus theft.
- **Per-turn identity chip**: agent name plus a stable non-human glyph and
  color; subagent turns nest under the delegating turn; anything run with
  user authority is stamped "on behalf of". TTS voice pairs 1:1 with the
  chip.
- **Structured decision cards**: approvals are never blocking modals and
  never free-text questions. Proposed action with the literal command or
  diff preview, then approve / reject-with-feedback. The same card renders
  twice from one source: as an attention item and inline in the transcript
  at the pause point. The user can keep scrolling the transcript and trace
  while judging.
- **The composer never locks, and a queued steer is dismissible**
  [CROSSPLAN 2026-08-16, F5, F7]. Enter during a run queues the steer and
  renders it as a chip at its insertion point; the chip's text is the
  newline join of the pending row's `parts`, since the row has no `content`
  field. Every chip carries a dismiss affordance, because the backend
  realizes a queued row only when the in-flight turn finishes CLEANLY: a
  failed, cancelled or interrupted turn leaves the row queued indefinitely
  (primer/session/dispatch.py:586, :648 versus :687). Design the chip as a
  removable pending item, not as a promise.
- **There is no "parked" session status** [CROSSPLAN 2026-08-16, F6]. The
  five lifecycle values are created, running, waiting, paused and ended.
  Parking is an orthogonal flag (`parked_status`: parked or resumable) that
  rides alongside them, so a parked session shows a `waiting` lifecycle plus
  a park marker. Do not draw a sixth status pill; draw a badge that can sit
  on top of `waiting`.
- **Rail discipline**: exactly three place-y lists (sessions, files,
  attention). Global utilities live in the top bar. Order, hiding, badge
  style and collapse persist per account.
- **Preview tabs**: VS Code semantics. Single click opens a reused italic
  preview tab; edit or double click promotes it; pinning is supported and
  the operator session is pinned by default; MRU cycling. Agent-driven
  opens always land in the preview tab.
- **Split editor groups**: at least two, with "Split Right" as a verb.
  Comparison never goes in an overlay: trace opens beside the transcript,
  diffs side by side.
- **Palette**: verb-noun Title Case, enforced at registration. Aliases are
  matched but the canonical label is displayed ("Park Session (Pause)").
  Ranking is base weight times fuzzy score times a destructive dampener,
  with hard context gating by the focused tab and a client-side frecency
  index. Every row shows its chord. A persistent Cmd+K chip advertises it.
  The composer's "/" affordance is the same registry.
- **Dual-render rule**: the palette is the router, but every verb also
  exists as a clickable affordance rendered FROM the registry (rail rows,
  context menus, overlay buttons, attention actions). Nothing is
  palette-only.
- **Hold-to-talk**: primary, with double-tap latch. Recording state is an
  unmissable composer border or waveform at a fixed position. Dictation
  always lands as editable text; release never auto-sends. Gated verbs
  always require explicit confirmation. TTS is per-turn opt-in plus a
  per-session "voice replies" toggle that auto-plays final answers only,
  with a persistent stop control.
- **Attention tiers**: interrupt (in-shell toast, spent sparingly) >
  ambient (rail badge, no sound) > digest (per-session collapsed rollup).
  Every item carries resolve, snooze and mute-session.
- **First-run walkthrough** IS the operator session: a seeded operator turn
  with a 3-5 step checklist whose steps are live verb invocations. No
  separate welcome page. Every empty state is a prompt with an action.

## Prohibited

Bare spinners or silent pre-first-token gaps; raw tool JSON inline;
force-follow scroll; focus-stealing agent opens; palette-only critical
actions; unranked palette; modal wizards for deep work; tab creep from
narration; docs without URLs; auto-playing TTS in text contexts or from
background sessions; open-mic auto-commit; human-passing agent identities;
flat interleaving of subagent turns; a rail used as a utility junk drawer;
onboarding sprawl.
