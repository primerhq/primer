# 60-second console demo — script + shot list

**Goal:** in one take, show the thing libraries can't — a *running system with an operator console*. Motion first, prose never. Silent screen capture with on-screen caption cards (works on X, Reddit, README, HN). Optionally a calm VO.

**Capture setup:** 1920×1080, dark theme, hide bookmarks bar, seed a workspace with the graph you already have (begin → planq → search → review → write → end). ~2× speed on any waiting. Target 55–65s. Export a GIF (README, ≤10MB) *and* an MP4 (X/LinkedIn).

| t | Shot | On-screen caption |
|---|---|---|
| 0–4s | Cold open on the **Studio graph run view**, the begin→…→end chain lit up | "Orchestrate fleets of small agents — not one giant prompt." |
| 4–12s | Kick a graph run; nodes light up left→right as agents claim + finish | "Each agent gets a clean, purpose-built context." |
| 12–22s | Click a node → transcript **filters to that node**; scroll its output | "Directed graphs: producer → judge → writer." |
| 22–32s | In Studio, **Mount collection** onto the running workspace — its docs appear as live files in the tree; agent edits one, then **Apply to collection** pops a diff; confirm and the edit lands upstream | "Knowledge lives as files your agent can edit — not a read-only blob." |
| 32–42s | Show an **approval / park-and-resume**: agent waits on a human; approve it; it resumes | "Park on a human decision — free the compute — resume when the reply lands." |
| 42–50s | Flash the **Agents / Graphs / Toolsets** pages or the MCP surface | "Self-hosted. MCP-native. Batteries included." |
| 50–60s | End card: logo + one-liner + `pipx install 'primer-ai[full]'` + repo URL | "Primer — github.com/primerhq/primer" |

**Rules of thumb:** no dead air (cut waits), one idea per caption, the last 3 seconds must show the install command + URL (most people only watch the end). Record 2–3 takes; the collection-mount sync (22–32s) and the park-and-resume moment (32–42s) are the two memorable beats — make sure both read clearly.

**Cut-downs to also export:**
- **6-sec loop** (the graph lighting up + one caption) — for X autoplay / README top.
- **15-sec** (graph run + park-and-resume only) — for Reddit inline.
