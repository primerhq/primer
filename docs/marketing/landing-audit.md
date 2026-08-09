# Landing page conversion audit — for the 0.3.0 launch

> Audit only. Nothing on the live site (`primerhq.github.io`) was edited. Grounded in
> `_refresh-brief-0.3.0.md` and `positioning.md`. All file paths below are relative to
> the site clone at `primerhq.github.io/`.

## 1. What the site is

- **Framework:** bespoke, not Jekyll/MkDocs/Docusaurus. Two independent halves:
  - **Landing page** — hand-authored static HTML/CSS/JS at the repo root: `index.html` (page),
    `home.css`, `home.js` (tab-switching, copy buttons, scroll reveals, the animated loop SVGs).
    No templating, no build step — you edit these files directly.
  - **Docs** — a real static-site generator (`build/build_site.py`, ~22KB) that renders
    Markdown from `docs_source/**/*.md` (driven by `docs_source/manifest.yaml`, which is the
    nav/IA) into `docs/**/index.html`. `docs/index.html` is just a redirect to
    `/docs/getting-started/introduction/`.
- **Hero location:** `index.html`, `<section class="hero" id="top">`, lines 57–119.
  - Eyebrow: line 60 — `Self-hosted agent orchestration`
  - H1: line 61 — `Build the loop.<br/>Trust the output.`
  - Subhead: line 62 — `An unopinionated, batteries-included agent orchestration platform you self-host — built so a small, clean context beats a big one.`
  - Primary CTA: lines 64–89 — two copyable install snippets (pipx, Docker) + a secondary `Read the docs →` button
  - Note under CTA: line 90 — "Then open the console at `http://localhost:8000/console/`"
- **Top nav:** `index.html` lines 37–43 — `#problem #loop #install #features` anchors + a `/docs/` link; `topbar__actions` (lines 45–50) duplicates a `Docs` link plus an icon-only GitHub link (no star count).
- **Final CTA:** `index.html` lines 329–360, `<section class="finalcta">` — same two install snippets + `Read the docs` / `View on GitHub` buttons.
- **Feature showcase (tabs):** content lives in `home.js` lines 114–132 (`FEATURES` / `SHOT_IMG` objects), rendered into the `#features` section of `index.html`.
- **Quickstart:** `docs_source/getting-started/quickstart.md` — a 6-step, full tutorial (provider → 2 agents → chat with agent-switch → internal collections + a router agent → workspace + file-writing agent → a producer/judge graph with park-resume). Realistically 20–30 minutes end to end, not a 5-minute path.

## 2. Audit findings

### Hero clarity vs. the real positioning — under-indexes on 2 of the 4 proof pillars
`positioning.md` names four pillars every asset should lean on: **(1) control plane, not a
library, (2) MCP-native, (3) graph engineering, (4) batteries-included & self-hostable.** The
hero currently only lands (3) and (4):
- The eyebrow says **"Self-hosted agent orchestration"** — not "control plane," not "fleets."
  A reader skimming the eyebrow + H1 could still mistake Primer for an `import`-able
  orchestration library, exactly the framing `positioning.md` says to escape.
- **MCP-native — the freshest, least-crowded hook per the brief — is completely absent from
  the hero.** It only shows up three sections down, as one tab among six in the feature
  showcase (`home.js` line 120) and one bullet in the "Loop" blocks list (`index.html` line 221).
- The subhead nails the context-bet thesis well (and correctly hedges it as a bet), but it
  never says *what kind of thing you get* — no "fleets," no "console," no "self-hosted control
  plane." A first-time visitor has to reach the `#problem` section (line ~122) before "control
  plane" appears anywhere, and it never appears verbatim on the page at all — the closest is
  "batteries-included agent orchestration platform."

### CTA strength — present but diffuse, and missing a star ask
- Above the fold there are **three competing actions** with no visual hierarchy telling a
  visitor which to take first: pipx snippet, Docker snippet, "Read the docs →". For a technical
  audience this is fine as a *set*, but nothing marks pipx as the recommended path (it's the
  one used everywhere else — quickstart, README, release notes).
- **No GitHub star ask anywhere on the page.** The only GitHub link in the hero/topbar
  (`index.html` line 47) is an icon-only button with an `aria-label`, no visible star count, no
  "Star on GitHub" text. `LAUNCH_PLAN.md` treats star velocity as the north-star metric — the
  landing page currently does nothing to convert a visit into a star.
- "Read the docs →" (line 88) goes to `/docs/` → redirects to `getting-started/introduction/`
  (a conceptual explainer), **not** to the quickstart. A visitor who wants to *do* something
  hits a "why context matters" essay before any hands-on step.

### 0.3.0 feature-set coverage — the headline feature is missing; version strings are stale
- **Collection ↔ Workspace mount — the 0.3.0 headline differentiator per the brief — does not
  appear anywhere on `index.html` or in `home.js`.** The "Collections" showcase tab
  (`home.js` line 117) still describes pre-0.3.0 semantic search only: *"Build knowledge
  collections with vector search so agents retrieve exactly what they need — and nothing they
  don't."* No mention of mounting a collection as a live editable directory, the 3-way diff, or
  "Apply to collection." This is the single biggest content gap versus the brief — it's called
  out there as belonging in "the README hero (a bullet)... the reddit/standout feature list,"
  and the same logic applies to the landing hero/showcase.
- Other 0.3.0-era pillars *are* already covered reasonably well: park-and-resume (`index.html`
  line 222, "Human gate"), graphs/DCG (whole `#loop` section), workspaces/isolation (`#selfhost`
  section, line 314), MCP (showcase tab + "Connectors" block line 221). Channels is mentioned
  only as a word in the "Connectors" bullet — its showcase tab was deliberately dropped
  (see `home/README.md`) because no populated screenshot exists yet.
- **Toolset connectivity guard** (the smaller 0.3.0 ops-posture beat) isn't mentioned at all —
  consistent with the brief's "don't over-feature it," but there isn't even a footnote near the
  MCP tab, which is where the brief suggests it belongs.
- **Stale `0.2.0` Docker tags, three times on the landing page** — `index.html` lines 85, 255,
  352 all pin `ghcr.io/primerhq/primer:0.2.0`. The same stale pin also appears in
  `docs_source/getting-started/quickstart.md:21`. This is a real bug, not just a copy nit: it
  ships people an old image on the same release where `/v1/health` was specifically fixed to
  report the true running version (per `docs/marketing/release-notes-0.2.0.md`'s own
  changelog note and the branch this repo is currently on). Two more stale `"version": "0.2.0"`
  strings sit in example JSON payloads at `docs_source/reference/api-workers-health.md:115` and
  `docs_source/features/observability.md:26` — cosmetic, but they now contradict the feature
  they're supposed to be demonstrating.

### Social proof — none
No stars badge, no download count, no testimonials, no "used by," no press mentions anywhere
on the page. Understandable pre-0.3.0-launch, but worth flagging against `LAUNCH_PLAN.md`'s own
Phase 0 checklist item ("Seed 30–50 honest stars from network" — ⏳) so a badge slot exists to
fill in the moment stars land, rather than being added later as an afterthought.

### Time-to-first-value — quickstart is thorough, not fast
`LAUNCH_PLAN.md` Phase 0 lists "<5-min quickstart proven to work" as a launch gate. The actual
quickstart (`docs_source/getting-started/quickstart.md`) is a genuinely good tutorial but it's a
6-step, ~20–30 minute build (provider, two agents, chat handoff, collections + router agent,
workspace + file write, a full producer/judge graph with a real park-resume). There is no
separate "hello world in 5 minutes" path, and the hero's only doc link routes through the
conceptual introduction first. Net effect: the <5-minute promise implied by the launch plan
isn't actually one click from the hero today.

## 3. Concrete before → after copy

### Hero rewrite (headline kept — it's genuinely strong and ownable; eyebrow + sub sharpened to land the missing pillars)

**Current** (`index.html` lines 60–62):
```html
<p class="eyebrow"><span class="dot"></span> Self-hosted agent orchestration</p>
<h1 class="hero__title">Build the loop.<br /><span class="accent">Trust the output.</span></h1>
<p class="hero__sub">An unopinionated, batteries-included agent orchestration platform you self-host — built so a small, clean context beats a big one.</p>
```

**Proposed:**
```html
<p class="eyebrow"><span class="dot"></span> Self-hosted control plane for agent fleets · MCP-native</p>
<h1 class="hero__title">Build the loop.<br /><span class="accent">Trust the output.</span></h1>
<p class="hero__sub">Primer is the self-hosted control plane for fleets of small, context-optimized agents — graphs, workspaces, triggers, and channels, wired together and run on your own hardware. The bet: a clean, purpose-built context lets a small model rival a much bigger one — a thesis, not a benchmark, so try it and tell us where it breaks.</p>
```
*(H1 unchanged by design — "Build the loop. Trust the output." already carries the loop-engineering
narrative well and shouldn't be touched. The eyebrow now says "control plane" and "fleets"
verbatim and surfaces MCP-native above the fold; the sub keeps the honest thesis framing the
brief requires.)*

**Primary CTA** — mark pipx as the recommended path and point the docs link at the quickstart, not the intro:
```html
<span class="install__label">pipx <em>(recommended)</em></span>
...
<a class="btn btn--secondary" href="/docs/getting-started/quickstart/">Try the 5-min quickstart →</a>
```

### Prioritized edits

| # | File / section | Current | Proposed | Why |
|---|---|---|---|---|
| 1 | `index.html` lines 85, 255, 352 + `docs_source/getting-started/quickstart.md:21` | `ghcr.io/primerhq/primer:0.2.0` | `ghcr.io/primerhq/primer:0.3.0` (or `:latest`) | Version-rule compliance, and a real bug: it currently ships the *old* image on the very release where `/v1/health` was fixed to report the true version. Also fix the two example JSON payloads at `docs_source/reference/api-workers-health.md:115` and `docs_source/features/observability.md:26` (`"version": "0.2.0"` → `"0.3.0"`). |
| 2 | `home.js` lines 117 & 126 (`FEATURES.collections`, `SHOT_IMG.collections`) | *"Build knowledge collections with vector search so agents retrieve exactly what they need — and nothing they don't."* | *"Mount a whole knowledge collection into an agent's workspace as a live, editable directory — the agent reads and writes the files directly, then a 3-way diff syncs edits back upstream with Apply to collection."* (swap in a real `internal-collections`/mount screenshot once captured, replacing the current `internal-collections-enable-dark.png` which only shows the enable step) | This is the 0.3.0 headline differentiator per the brief and it's currently invisible on the landing page — the Collections tab still describes 0.2.0-era semantic search only. |
| 3 | `index.html` lines 60–62 (hero eyebrow/sub) | See rewrite above | See rewrite above | Hero currently misses 2 of the 4 named proof pillars (control plane, MCP-native) — a skimming visitor can leave thinking this is a library, not a platform they run. |
| 4 | `index.html` line 88 (`Read the docs →`) and line 356 (final CTA) | `href="/docs/"` (redirects to the conceptual introduction) | `href="/docs/getting-started/quickstart/"` for the hero button; keep the final-CTA one pointed at `/docs/` since by then the visitor has read the whole page | Closes the gap between "click CTA" and "do something" — the intro is a good read but isn't the fastest path to a working agent. |
| 5 | `index.html` line 47 (GitHub icon link) | Icon-only, `aria-label="Primer on GitHub"`, no visible count | Add a visible "Star" button/badge next to it (e.g. a shields.io live star-count badge) once `LAUNCH_PLAN.md`'s "seed 30–50 stars" step is done | Star velocity is the plan's own north-star metric; the page currently has zero mechanism to convert a visit into a star. Use a real auto-updating badge — never a fabricated number (voice rule). |
| 6 | `index.html` footer (line 383) | `Self-hostable · runs on your hardware · source on GitHub` | `Self-hostable · Apache-2.0 · runs on your hardware · source on GitHub` | Apache-2.0 / no-lock-in is a named proof point for the indie ICP in `positioning.md` and currently appears nowhere on the landing page at all. |

## 4. Prioritized punch-list

**P0 — do before/at 0.3.0 launch (cheap, high-impact, some are factual bugs):**
1. Fix the three stale `:0.2.0` Docker tags on the landing page + the one in `quickstart.md`
   (edit #1). Shipping the old image the same week `/v1/health` was fixed to report the real
   version is an easy, embarrassing catch for a sharp-eyed HN/Reddit reader.
2. Add Collection ↔ Workspace mount to the landing page (edit #2) — the 0.3.0 headline feature
   is currently absent from the one page most launch traffic will land on first.
3. Sharpen the hero eyebrow + subhead to land "control plane" and "MCP-native" (edit #3) — both
   are named as top, freshest hooks in `positioning.md` and neither appears above the fold today.

**P1 — high-value, slightly more work:**
4. Point the hero/nav CTA at the quickstart, not the intro (edit #4); and/or add a genuinely
   ≤5-minute "hello world" fast path distinct from the full 6-step tutorial, to actually satisfy
   `LAUNCH_PLAN.md`'s own "<5-min quickstart" launch gate.
5. Land the demo GIF/video in the hero visual the moment it's captured (`assets/demo-script.md`
   is already written and calls out the collection-mount-sync and park-resume beats as the two
   memorable moments) — replace/supplement the current static SVG loop diagram with real product
   motion.
6. Add a real, auto-updating GitHub star badge (edit #5) once stars are seeded per
   `LAUNCH_PLAN.md` Phase 0 — cheapest possible social-proof addition, and honest (no invented
   numbers).

**P2 — polish, do when convenient:**
7. Mention Apache-2.0 explicitly (edit #6) — cheap trust signal, currently absent site-wide.
8. Add a nav anchor for the `#recipes` (Cookbook) section — the page has the content but the
   top nav (`Problem / Loop / Install / Features`) doesn't link it.
9. Add a one-line footnote near the MCP showcase tab for the toolset connectivity guard (the
   smaller 0.3.0 ops-posture beat) — per the brief, "a footnote elsewhere," which the MCP tab
   is the natural home for.
10. Restore the Channels showcase tab once a populated (non-empty-state) screenshot exists —
    it's a named pillar in the dual-ICP matrix in `positioning.md` but currently has zero visual
    proof on the landing page (see `home/README.md` for exactly what's needed to re-add it).
