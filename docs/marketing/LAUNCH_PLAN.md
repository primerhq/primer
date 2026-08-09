# Primer 0.3.0 — Launch & Growth Plan (living doc)

> Local, gitignored working doc. Not committed. Owner: @usmanshahid.
> Status legend: ✅ done · ✍️ drafted (needs your review/publish) · ⏳ waiting on you · ⬜ not started

---

## 0. North-star & 90-day targets
Optimize early for **star velocity** — it gates HN/Reddit ranking, credibility, and awesome-list acceptance.

| Metric | 30d | 90d |
|---|---|---|
| GitHub stars | 300–500 | 1.5–3k |
| PyPI installs (`primer-ai`) | 500/wk | 2k/wk |
| Discussions / Discord members | 50 | 300 |
| Design-partner / eval conversations | 5 | 20 |

## 1. Positioning (see `positioning.md` for the full matrix)
- **Core bet (from the README):** *a small model given a clean, purpose-built context can rival a much larger one.*
- **Category reframe:** not another framework you `import` — a **self-hosted control plane you run** for fleets of small agents.
- **Two ICPs, two messages:** indie AI builders (speed, self-host, MCP, a real console) · platform/infra teams (control plane, isolation, ops, production posture).
- **Freshest hook:** **MCP-native** — operate Primer *with* agents. Least-crowded, most timely.
- **Ownable narrative:** **graph engineering.**

## 2. Phased sequence (build credibility → spend the big shot last)

### Phase 0 — Foundations (Week 1): don't launch naked
| Item | Status |
|---|---|
| Sharpened one-liner + README hero | ✍️ `assets/README-hero.md` |
| Release notes + GitHub Release (v0.3.0 Latest, v0.2.0 backfilled) | ✅ done |
| Repo description (GitHub) | ✅ done (owner) |
| 60-sec console demo video | ⏳ you (script: `assets/demo-script.md`) |
| Landing page (`primerhq.github.io`) conversion audit | ✍️ in progress — docs-site clone now exists locally; Phase-0 audit underway → `docs/marketing/landing-audit.md` |
| <5-min quickstart proven to work | ⏳ you (sanity-check on a clean box) |
| Comparison page | ✍️ `articles/comparison.md` |
| Enable GitHub Discussions | ⏳ you (repo Settings → Features) |
| Awesome-list submissions | ✍️ `social/awesome-lists.md` (you open PRs) |
| Seed 30–50 honest stars from network | ⏳ you |

> **Note:** all launch drafts — `articles/comparison.md`, `social/awesome-lists.md`, this tracker, and the rest of the kit — were refreshed for 0.3.0.

### Phase 1 — Content anchor (Week 1–2)
- Publish canonical on **own blog/landing** (SEO), then syndicate to **Medium + dev.to + LinkedIn**.
- Two pieces: the **launch post** and the **loop-engineering manifesto** (flagship, evergreen).
- Files: `articles/manifesto.md`, `articles/launch-post.md`.

### Phase 2 — Reddit (Week 2–3), staggered 2–4 days apart
- Tailored per sub, value/story-first. File: `social/reddit.md`.

### Phase 3 — Show HN (Week 3–4): the peak lever
- After soft launch irons out first impressions. File: `social/show-hn.md`.

### Phase 4 — Sustain (ongoing)
- Release beats (0.3.0 is live now — release notes + GitHub Release published), cookbook→tutorials, comparison SEO, community.

## 3. Channel → ICP map
| Channel | ICP | Notes |
|---|---|---|
| Show HN | both | biggest single lever; one good shot |
| r/LocalLLaMA, r/LLMDevs, r/selfhosted, r/Python, r/MachineLearning | indie | staggered, tailored |
| X/Twitter thread | indie | demo video leads |
| LinkedIn | platform teams | ops/production framing |
| Medium / dev.to | both | syndication, not primary traffic |
| awesome-* lists, MCP directories | both | cheap, durable, SEO |

## 4. Metrics & instrumentation
- GitHub stars (daily), PyPI download stats (pypistats), `/console` referrers, Discussions/Discord signups.
- UTM-tag every outbound link to the landing page so you can see which channel converts.

## 5. Risks / do-nots
- Don't blow the HN shot on an unpolished repo — sequence it **last**.
- Don't paste identical text across subreddits (bans + spam signal).
- Don't over-claim beyond what 0.3.0 truly is — HN/Reddit punish hype.
- Don't launch without being present to respond for 8+ hours.

## 6. Execution tracker — who does what
**I draft (you review + publish):** positioning/one-liner, README hero, demo script, manifesto, launch post, comparison page, Reddit posts, Show HN post + first comment, LinkedIn/X copy, awesome-list entries, launch-day checklist.

**Only you can:** record the demo video; publish to Medium/LinkedIn/Reddit/HN; enable Discussions; open awesome-list PRs; ~~apply the repo description~~ (done); seed stars; sanity-check the quickstart on a clean machine.
