# Where to publish + distribute Primer articles (for developer adoption)

Research date: 2026-07. Goal: GitHub stars + people trying Primer. Content: mix of
technical deep-dives and opinion/thought-leadership. Scope: publishing platforms +
distribution channels + a combined playbook.

## The one big strategic insight

Primer is **open-source, self-hosted, privacy-first, and about running local /
small models on your own hardware.** That is *exactly* the profile the highest-reach
developer channels overindex on. Hacker News "really likes and overindexes on
open-source, privacy-first products"; r/LocalLLaMA and r/selfhosted are built around
local models and self-hosting. So the channels with the widest reach are also the
best *fit* here - you are not fighting the audience. Lean into that, hard.

Medium is the weakest fit for this goal: its paywall blocks non-members, its
Partner-Program changes through 2025-26 cut reach for legitimate writers, and its
discovery has eroded. Keep Medium only as an optional syndication mirror, never the
home.

---

## Part A - Publishing platforms (where the article LIVES)

Ranked for developer adoption of an OSS self-hosted tool.

| Platform | Fit | Reach / discovery | SEO + canonical | Verdict |
|---|---|---|---|---|
| **Own blog** (Astro/Hugo/Next on your domain, e.g. blog.primerhq.*) | Highest control | None on its own - you drive traffic to it | **Your SEO source of truth.** Owns the canonical URL; compounds over time | **Home base.** Publish here first as the canonical version |
| **Dev.to** | Excellent - huge, active dev community | **Best built-in feed discovery** of the platforms; immediate exposure to a large dev audience; tag ecosystem (#ai #opensource #llm #devtools) | Supports `canonical_url` -> point it at your blog so SEO credit stays home | **Primary syndication target.** Community reach without splitting SEO |
| **Hashnode** | Good - code-centric audience; custom domain | Weaker feed discovery than Dev.to (do not expect Dev.to-level traffic from the feed) | Custom-domain blogging + canonical support; can itself be your source-of-truth if you do not want to run a static site | **Alternative home base** if you would rather not self-host a blog |
| **Substack / Ghost / Beehiiv** | Good for a *newsletter*, weak for tool discovery | Owned-audience play, not dev-search discovery | Fine, but not a dev-SEO engine | **Later** - start one only when you have a following to nurture |
| **Hacker Noon / freeCodeCamp** | Niche/editorial | HackerNoon has some reach but mixed quality signal; freeCodeCamp is editor-gated (hard to get in, high credibility if you do) | Both accept canonical cross-posts | **Optional** extra syndication; freeCodeCamp only for a truly strong tutorial |
| **Medium** | Weak for this goal | Paywall + weakened distribution | Canonical supported, but you are feeding a walled garden | **Skip or mirror only** |

**The platform rule:** own the canonical URL on a domain you control; treat every
other platform as syndication with `canonical_url` pointing home, so you get their
audience without splitting search ranking. Do NOT verbatim-duplicate across many
platforms without canonical tags - that's what causes ranking loss; canonical fixes it.

---

## Part B - Distribution channels (where you POST to reach people)

| Channel | Fit for Primer | Realistic reach | Rules / how to not get removed |
|---|---|---|---|
| **Hacker News - Show HN** | **Best single fit.** HN overindexes on OSS + self-hosted + privacy | **Biggest spike:** front page = 5k-30k visits in 24h, ~500-2000 GitHub stars for an OSS launch | Post Tue-Thu 08:00-10:00 PT; first ~15 min of upvotes decide it; title direct + specific; link to a repo/live thing they can poke; **first comment = you, the maker**, with the story + one honest limitation. One-time pulse, not a growth channel |
| **r/LocalLLaMA** | **Perfect niche** (local/small models) | Large, highly-relevant | Self-promo tolerated but policed: frame as a lesson/contribution with the tool as context, keep promo under ~10% of your activity; closed/paid pitches get pushback |
| **r/selfhosted** | **Perfect niche** (self-hosters, strong OSS preference) | Large, relevant | Same ethos - lead with "here's a thing I built and how it works," not "check out my product" |
| **r/opensource, r/LLMDevs, r/artificial** | Good secondary | Medium | Limited self-promo allowed; read each subreddit's rules/promo-thread - non-negotiable, mods ban rule-skippers |
| **r/MachineLearning** | Weaker (research-leaning, stricter) | Large but harder | High bar; only a genuinely novel technical piece |
| **Lobsters (lobste.rs)** | High-signal dev audience | Smaller than HN but quality | Invite-only; self-promo must be **< ~25%** of your activity and stand on its own (technical writeup, architecture, postmortem). Great for deep-technical pieces |
| **Console.dev** | **Standout free opportunity** | Weekly devtools newsletter, reviews 2-3 tools/week, engaged devtool audience | Submit your tool via their site; editorial pick, not paid. **Do this.** |
| **TLDR (dev / AI)** | Broad | ~470k subscribers | Mostly sponsorship for inclusion (paid); organic mentions possible if you hit HN/GitHub-trending |
| **The Changelog (News)** | Good | Dev news + hot GitHub repos + podcast | Submittable; a strong OSS launch or a good writeup can get picked up |
| **daily.dev** | Good passive | Aggregator + own publishing surface; large dev reach | Add your blog as a source / squad; low-effort compounding |

The universal Reddit/Lobsters rule: **the 9:1 rule** - for every ~10 contributions,
at most 1 is self-promo. Build a small track record of genuine comments first;
lead with substance (a lesson, a build story, a benchmark), tool as context.

---

## Part C - The playbook (write once, reach everywhere)

**Per article - the syndication chain:**
1. **Publish canonical on your own blog/domain** (SEO source of truth).
2. **Cross-post to Dev.to** with `canonical_url` -> your blog (community reach, no SEO split). Tag well (#ai #opensource #llm #selfhosted #devtools).
3. **Optionally mirror** to Hashnode / Medium with canonical too, if cheap.
4. **Add your blog to daily.dev** as a source once (compounds passively).
5. **Distribute** the link to the right *one or two* channels for that piece (below), not all at once.

**Match the piece to the channel:**
- *Technical deep-dive / tutorial* (e.g. "running an agent loop on a 12B local model", "how directed cyclic graphs converge") -> Dev.to + **Lobsters** + **r/LocalLLaMA** / r/selfhosted.
- *Opinion / narrative* (the small-model bet, graph engineering, why context beats model size) -> Dev.to + **Hacker News** (as a blog-post submission, not Show HN) + r/LocalLLaMA discussion.

**Prioritize these 5 first (in order):**
1. **Hacker News (Show HN)** - once, for the flagship "Primer launch" moment.
2. **r/LocalLLaMA** and **r/selfhosted** - your two home communities; participate genuinely, then share.
3. **Dev.to** - the reliable syndication home for every article.
4. **Console.dev** - submit Primer (free editorial devtools review).
5. **Lobsters** - for the deep-technical pieces specifically.

**Cadence:** a technical or opinion piece every ~1-2 weeks keeps a Dev.to/blog
presence warm; save the **Show HN** for a genuine milestone (a flagship writeup + a
polished repo/demo). Don't burn HN on a thin post.

---

## Flagship launch-day sequence (the Show HN pulse)

1. **Pre:** repo README is tight, there's a live demo or a 30-sec quickstart, and the flagship blog post is already published (canonical) + on Dev.to.
2. **08:00-10:00 PT, Tue-Thu:** submit `Show HN: Primer - <one crisp line>` linking to the repo (or the demo).
3. **Immediately:** post your maker first-comment: what it is, why you built it (the small-model bet), the stack, and **one honest limitation** ("the small-model bet is still a bet").
4. **First 15-60 min:** be present, answer every comment fast and substantively (this window decides front-page).
5. **Same day, staggered (not simultaneous):** share the *blog post* (not the HN link) to r/LocalLLaMA and r/selfhosted, framed as "I built this / here's how it works," and drop it in your Dev.to network.
6. **After:** submit to Console.dev and The Changelog; the HN/GitHub-trending bump makes organic newsletter pickup more likely.

---

## Failure modes to avoid

- **Spray-and-pray self-promo:** posting the same "check out my project" link to 8
  subreddits in a day -> removals + bans. Lead with substance; respect the 9:1 rule.
- **Wrong subreddit / no track record:** dropping into r/MachineLearning cold, or
  ignoring a subreddit's promo-thread rule.
- **Thin content on HN:** Show HN is a one-shot; a weak post wastes the one big pulse.
- **Verbatim duplication without canonical tags:** splits SEO and can tank organic
  traffic. Always set `canonical_url` back to your blog.
- **Treating any platform as a write-only megaphone:** HN, Reddit, and Lobsters all
  punish this. Participate as a member first.
- **Medium as the home:** paywall + weak distribution makes it the wrong source of
  truth for adoption.

---

## Sources
- Best Medium alternatives 2026 - https://blog.fika.bar/best-medium-alternatives-in-2026-01KNVCAKQ23AHZYHNZE89S7W53
- Substack vs Medium 2026 - https://distribb.io/blog/substack-vs-medium-the-ultimate-guide-for-writers-2025-edition
- Hashnode vs Dev.to 2026 - https://www.misar.blog/compare/hashnode-vs-devto
- Cross-posting Dev.to/Medium/Hashnode - https://dasroot.net/posts/2026/03/cross-posting-technical-content-devto-medium-hashnode/
- Own your work with canonical tags - https://mikebifulco.com/posts/own-your-work-with-canonical-tags
- HN marketing for dev tools (daily.dev) - https://business.daily.dev/resources/hacker-news-marketing-developer-tools-show-hn-launch-day-sustained-coverage/
- How to launch a dev tool on HN - https://www.markepear.dev/blog/dev-tool-hacker-news-launch
- HN front page 2026 playbook - https://www.flowjam.com/blog/how-to-get-on-the-front-page-of-hacker-news-in-2025-the-complete-up-to-date-playbook
- Can I post my startup on r/LocalLLaMA - https://www.launchwake.com/channels/r-localllama
- Reddit self-promotion rules DB - https://oneup.today/tools/reddit-self-promotion-checker
- Best subreddits for sharing your project - https://tereza-tizkova.medium.com/best-subreddits-for-sharing-your-project-517c433442f9
- Console.dev - https://console.dev/
- Developer newsletters list - https://github.com/jackbridger/developer-newsletters
- TLDR dev - https://tldr.tech/dev
- Lobsters about - https://lobste.rs/about
- Lobsters self-promo meta - https://lobste.rs/s/tnpfea/meta_could_we_consider_banning_self
