# UI: the Studio2 trial shell

An OPT-IN parallel console at `#/studio2`, running the consolidated
Studio design (one persistent shell; every feature a navigator plus
documents; every verb in one command registry) alongside the classic
console, which stays byte-for-byte untouched except one "Studio (trial)"
sidebar entry. The trial exists to validate the consolidation before any
cutover; nothing classic is removed.

Design rationale lives in the (local, not committed) spec
`docs/superpowers/specs/2026-08-08-studio-consolidation-design.md` and
its design-quality companion
`2026-08-09-studio-design-pack-analysis.md`.

## The root gate

`ui/app.jsx` mounts `S2_RootGate`: when the hash path starts with
`/studio2` it renders `window.S2_Shell` INSTEAD of `App`; otherwise the
classic console renders unchanged. One unconditional hook, so hook
order is stable across navigation.

## Registries (`ui/components/studio2/`)

- `S2_Commands` (`s2-commands.jsx`): `register({id, title, glyph, cat,
  shortcut?, when?, run})`, `list()`, `run(id)`. ONE verb table feeds
  the palette, the menu bar (`S2_Menus`, generated per category), and
  the `g`+letter chords. A shortcut is never defined twice.
- `S2_Docs` (`s2-docs.jsx`): `registerKind(kind, {glyph, title(ref),
  render(ref, docApi)})`, `open/activate/close`, dirty tracking, tab
  persistence in `localStorage["studio2:tabs"]`, and the active tab
  mirrored to `#/studio2?open=<kind>:<ref>` (deep links restore on
  load). `docApi = {setDirty, close}`.
- `S2_Ctx` (`s2-ctx.jsx`): the derived workspace context. It follows
  the active SESSION document unless pinned via the palette
  (`Context: pin to <ws>` / `Context: follow active tab`); it scopes
  the Files navigator, and the right rail. There is no manual selector.
- `S2_QuickIndex` (`s2-nav.jsx`): the quick-open noun table (agents,
  sessions, classic routes), read from the live `useResource` cache.

## The legacy bridge

Un-migrated features open as SAME-ORIGIN iframes of the classic console
(`s2-legacy.jsx`, kind `legacy`), so the trial covers the whole console
IA from day one. Enablers in `primer/api/_app_middleware.py`: the
console CSP carries `frame-src 'self'` + `frame-ancestors 'self'` and
`/console` responses send `X-Frame-Options: SAMEORIGIN` (cross-origin
framing stays blocked in both directions). Legacy iframes forward their
`keydown` events to `window.S2_handleKeydown` so shell shortcuts work
regardless of which document has focus. A feature migrates natively by
registering its document kind (later files override the interim
registrations) and deleting its row from `S2_LEGACY_ROUTES`.

## Native documents so far

- Sessions (`s2-doc-session.jsx`): re-houses the workspace Studio's
  `SessionAgentPanel` / `SessionGraphPanel` (transcript stream,
  pause/resume/cancel, composer, graph run view) under the document
  contract. Feeds `S2_Ctx`.
- Agents (`s2-doc-agent.jsx`): the entity-form template. Post-cutover
  shape (one `model.profile_id` picker), dirty + Mod+S save, 422s map
  to inline field errors, `harness_id` rows render read-only.
- The right rail (`s2-right.jsx`) reuses `AttentionBar` and
  `WorkspaceTap` scoped to the context workspace; a deployment-wide
  feed needs a global endpoint (stated in the empty state).

## Migrating the next feature

Per feature: a navigator section in `s2-nav.jsx`, a document kind file
(copy the session/agent templates), palette commands, quick-index rows,
a keyboard-only Playwright journey, and delete the legacy row. Every
new `.jsx` file needs an `index.html` script tag; the
`test_bundle_transpiles_every_studio2_file` gate fails on a forgotten
tag.

## Testing

Static suites `tests/ui/test_studio2_*.py` (source checks + the bundle
transpile gate) run per change; Playwright journeys
(`tests/ui_e2e/test_studio2_shell_journey.py`,
`test_studio2_agent_keyboard_journey.py`) run against a live server.
The keyboard journey is the enforcement net for the no-mouse-only-
affordance rule: it creates an agent end to end without a single click.
