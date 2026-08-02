<!-- GitHub Release body for v0.2.0 (curated from v0.1.0..v0.2.0: 149 feat + 122 fix, 0 breaking).
     Reusable for v0.2.1 with a one-line note (see bottom). Publish with:
       gh release create v0.2.0 --title "v0.2.0" --notes-file <this> --verify-tag  -->

## Primer 0.2.0

The first substantial release since 0.1.0 — **149 features, 122 fixes, no breaking changes.** The headline: Primer grew from a single-user core into a **multi-user, self-hostable platform with a real operator console**.

### 👥 Multi-user & auth
- **RBAC** across the API — `require_user` / `require_admin` on every router, plus WebSocket/stream role gates (`4403 forbidden_role`).
- **OIDC SSO** — login + callback (strict `(provider_id, subject)` match, JIT provisioning, PKCE/state), authenticated **account linking**, and admin CRUD for OIDC providers.
- **User & token admin** — admin users CRUD (with last-admin anti-lockout), view/revoke any user's API tokens, self-service password change, first-account-becomes-admin bootstrap.

### 🖥️ Operator console (Studio)
- **Live workspace tap** (SSE) and a **graph run view** with per-node state snapshots.
- **In-browser terminal** into a workspace over WebSocket (local PTY host + runtime proxy).
- **Workspace file browser** — tree, read (mtime/etag), write with `412` preconditions, move/rename.
- Aggregated **pending-yields / action-required** surfacing, admin OIDC/SSO pages, and a large pass of console UX polish.

### 🔀 Graphs
- An **`ExecutionContext`** model + builder threaded through runs.
- New template filters (`fromjson`, `strip_fences`) and `graph_transition` events emitted into the session log.

### 💬 Chats & sessions
- **Cancel** a running turn, **rewind** history (with a compaction guard), per-turn **`response_format`**, agent **switch/handoff** markers, optional **session names**, **restart/reopen** an ended session, and tail-paged history for incremental load.

### 🤖 Agents
- Surface-aware **system-prompt rendering** against `ExecutionContext`, per-agent **`max_output_tokens`** cap, and workspace-surface context.

### 📦 Packaging
- **Slimmer default install** — heavy backends (HuggingFace, Docling, LanceDB, channels, k8s) now behind extras; **fat + slim Docker images** published to GHCR.

### Install
```bash
pipx install 'primer-ai[full]'                              # or the lean core: pipx install primer-ai
docker run --rm -p 8000:8000 ghcr.io/primerhq/primer:latest
```
Console at `/console/`. See the [README](https://github.com/primerhq/primer#quickstart) for the Postgres path.

**Full diff:** https://github.com/primerhq/primer/compare/v0.1.0...v0.2.0

---
<!-- For v0.2.1: prepend — "### 🩹 0.2.1 patch\n- fix: /v1/health and startup logs now report the real released version (was frozen at 0.1.0)." — and change the compare link to v0.2.0...v0.2.1. -->
