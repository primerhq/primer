# Awesome-list & directory submissions — Primer

Facts grounded in `docs/marketing/positioning.md` and `README.md`. Submission
formats below were checked against each target's current README/CONTRIBUTING
as of this writing (2026-07) — these lists change their rules and formats
fairly often, so re-verify the live CONTRIBUTING immediately before opening
each PR.

**General etiquette across all of these:**
- One entry per PR. Don't bundle multiple list submissions into one PR.
- Keep entries in alphabetical order within their category — most of these
  lists lint for this and will bounce a PR that isn't sorted.
- Follow each repo's own CONTRIBUTING.md/contributing.md exactly; several of
  these have a non-obvious submission mechanism (see awesome-selfhosted
  below — it is *not* a direct README edit).
- Space submissions out rather than opening all five PRs the same day — it
  reads better to reviewers and gives you time to fix formatting nits from
  the first one before repeating them elsewhere.

---

## awesome-mcp-servers (punkpeye/awesome-mcp-servers)

**Format:** `- [org/repo](url) <platform emoji tags> - Description.` Entries
carry small emoji badges indicating platform/OS support (local, cloud, macOS,
Windows, Linux, etc.).

**Suggested entry:**
```
- [primerhq/primer](https://github.com/primerhq/primer) - Self-hosted control plane for fleets of small, context-optimized agents; ships a built-in MCP server and client, plus collection-to-workspace mounts for agent-editable knowledge.
```

**Notes:**
- Confirm the current emoji legend in the README before adding badges, so
  they accurately reflect what Primer supports (self-hosted, local/container/
  Kubernetes).
- Place alphabetically under the closest-fitting existing category — the list
  runs 40+ categories and reorganizes periodically; check the current table
  of contents rather than assuming one from this note.
- High PR volume (large, popular list) — expect review to take a while.

---

## awesome-ai-agents (e2b-dev/awesome-ai-agents)

**Format:** each entry is a `## [Name](link)` header, a one-line description,
and a collapsible `<details>` block (category / description bullets / links),
kept alphabetical within category.

**FOUNDER JUDGMENT CALL:** this list's "Open source projects" section is
scoped to agents/assistants themselves. The maintainers point SDK/framework/
tooling submissions to a *sibling* list, "Awesome SDKs for AI Agents,"
instead. Primer is a platform for building and running agents rather than an
agent product itself — worth checking which of the two lists is actually the
better fit before opening a PR.

**Suggested entry (for whichever list fits):**
```
## [Primer](https://github.com/primerhq/primer)
Self-hosted control plane for fleets of small, context-optimized agents — graphs, workspaces (mountable knowledge collections), channels, MCP.
```

**Submission:** PR (alphabetical, correct category), or their intake form:
https://forms.gle/UXQFCogLYrPFvfoUA

---

## Awesome-LLMOps (tensorchord/Awesome-LLMOps)

**Format:** 3-column markdown table —
```
| [Name](url) | Description | ![GitHub Badge](https://img.shields.io/github/stars/ORG/REPO.svg?style=flat-square) |
```

**Suggested entry row:**
```
| [Primer](https://github.com/primerhq/primer) | Self-hosted control plane for fleets of small, context-optimized agents: graphs, workspaces, collections, channels, triggers, MCP. | ![GitHub Badge](https://img.shields.io/github/stars/primerhq/primer.svg?style=flat-square) |
```

**Notes:**
- No dedicated "Agent"/"Orchestration" category as of this writing;
  comparable orchestration frameworks (LangChain, LlamaIndex, Haystack) sit in
  the main LLMOps/application section. Place Primer near them, but check the
  current table of contents for a closer-fitting section first.
- Process per `contributing.md`: one PR per suggestion, alphabetical order
  within category, run their star-badge generation script before submitting.

---

## awesome-selfhosted

**Not a direct README PR.** The awesome-selfhosted README is generated from
structured data in a separate repo, **awesome-selfhosted-data**. To submit:
add a new YAML file under `software/` in that repo (kebab-case filename),
based on the template at `.github/ISSUE_TEMPLATE/addition.md`, with fields for
name, description, `website_url`, `source_code_url`, `licenses`, `tags`, etc.
(You can also open a GitHub issue there instead of a PR if you'd rather not
write the YAML yourself.)

Repo: https://github.com/awesome-selfhosted/awesome-selfhosted-data

**Rendered entry** (what it will look like once the README is generated from
the YAML, matching the list's existing format):
```
- [Primer](https://github.com/primerhq/primer) - Self-hosted control plane for fleets of small, context-optimized agents: graphs, workspaces, collections, channels, triggers, approvals. (`Source Code`) `Apache-2.0` `Python/Docker`
```

**Note:** in single-page mode, an entry only displays under the *first* tag
listed in its `tags` field — pick that primary tag carefully. Check the
current tag taxonomy in awesome-selfhosted-data before choosing one.

---

## MCP directory: mcp.so

**Format:** no markdown entry — submission is a GitHub issue (via the
"Submit" link on mcp.so, or their GitHub issues page directly) with the
server's name, description, and repo/connection info.

**Suggested issue:**
- Title: `Add Primer`
- Body:
  ```
  Primer - self-hosted control plane for fleets of small, context-optimized agents. Ships a built-in MCP server (exposes the full platform tool surface, including mounting a knowledge collection into an agent's workspace as a live, editable directory) and an MCP client. Apache-2.0. https://github.com/primerhq/primer
  ```

**Alternative, heavier path — the official MCP Registry**
(registry.modelcontextprotocol.io): not a one-line entry either. It requires
publishing a `server.json` manifest via the `mcp-publisher` CLI, and expects
the MCP server to already be published as an installable package (e.g. to
PyPI) under a namespace like `io.github.primerhq/primer`.

**FOUNDER JUDGMENT CALL:** Primer's MCP server ships as part of the larger
platform rather than as a standalone installable MCP-server package, so it's
worth deciding whether the official registry is the right fit yet, or whether
directory-style listings (mcp.so, and similarly Smithery/PulseMCP/Glama, which
mostly crawl and index rather than require a formal submission) make more
sense for now.
