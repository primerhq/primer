#!/usr/bin/env bash
#
# awesome-submit.sh — scaffold the Primer awesome-list / directory submissions.
#
# What it does: forks + clones each target repo under ./awesome-submissions/,
# creates a branch, and drops a SUBMIT.md with the EXACT entry and where to put
# it. It deliberately does NOT auto-insert into these lint-strict, alphabetical
# lists — a wrong position gets the PR bounced, and a human eye on placement is
# the only tedious part left. You paste one line, then run the printed PR command.
#
# Requirements: `gh auth status` must be logged in as the account that will own
# the PRs. Run from anywhere; everything lands in ./awesome-submissions/.
#
# Etiquette (already baked into the plan): ONE entry per PR, keep entries
# alphabetical within their category, follow each repo's CONTRIBUTING exactly,
# and SPACE the PRs out over several days rather than firing all five at once.
set -euo pipefail

ROOT="$(pwd)/awesome-submissions"
BR="add-primer"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$ROOT"

command -v gh >/dev/null || { echo "❌ gh CLI not found — install GitHub CLI first."; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "❌ not logged in — run: gh auth login"; exit 1; }

fork_clone () {  # <owner/repo> <entry-file-hint> <placement-note>
  local repo="$1" filehint="$2" note="$3"
  local name="${repo##*/}" dir="$ROOT/$name"
  echo; echo "=== $repo ==="
  if [ -d "$dir" ]; then
    echo "  already cloned at $dir — skipping fork/clone"
  else
    gh repo fork "$repo" --clone --fork-name "$name" -- "$dir" 2>/dev/null \
      || gh repo fork "$repo" --clone -- "$dir"
  fi
  git -C "$dir" checkout -B "$BR" >/dev/null 2>&1 || true
  {
    echo "# Primer submission — $repo"
    echo
    echo "Target file(s): $filehint"
    echo "Placement: $note"
    echo
    echo "Entry to add (paste, keep the section alphabetical):"
    echo
    echo '```'
    cat "$HERE/_entry_${name}.txt" 2>/dev/null || echo "(see docs/marketing/social/awesome-lists.md)"
    echo '```'
    echo
    echo "Then:"
    echo "  cd \"$dir\""
    echo "  # edit $filehint, place the entry, save"
    echo "  git add -A && git commit -m 'Add Primer'"
    echo "  git push -u origin $BR"
    echo "  gh pr create --repo $repo --title 'Add Primer' --body-file SUBMIT.md --web"
  } > "$dir/SUBMIT.md"
  echo "  ✅ ready: $dir  (see $dir/SUBMIT.md)"
}

# --- entry snippets (kept next to this script so SUBMIT.md can embed them) ----
cat > "$HERE/_entry_awesome-mcp-servers.txt" <<'EOF'
- [primerhq/primer](https://github.com/primerhq/primer) - Self-hosted control plane for fleets of small, context-optimized agents; ships a built-in MCP server and client, plus collection-to-workspace mounts for agent-editable knowledge.
EOF
cat > "$HERE/_entry_awesome-ai-agents.txt" <<'EOF'
## [Primer](https://github.com/primerhq/primer)
Self-hosted control plane for fleets of small, context-optimized agents — graphs, workspaces (mountable knowledge collections), channels, MCP.
EOF
cat > "$HERE/_entry_Awesome-LLMOps.txt" <<'EOF'
| [Primer](https://github.com/primerhq/primer) | Self-hosted control plane for fleets of small, context-optimized agents: graphs, workspaces, collections, channels, triggers, MCP. | ![GitHub Badge](https://img.shields.io/github/stars/primerhq/primer.svg?style=flat-square) |
EOF

# --- the three true fork -> edit -> PR targets --------------------------------
fork_clone "punkpeye/awesome-mcp-servers" \
  "README.md" \
  "under the closest-fitting category (check the live TOC — 40+ categories); confirm the platform emoji legend and append the right badges."

fork_clone "e2b-dev/awesome-ai-agents" \
  "README.md (Open-source projects)" \
  "JUDGMENT CALL: this list scopes 'Open source projects' to agent PRODUCTS; SDKs/frameworks/platforms are pointed to the sibling 'Awesome SDKs for AI Agents'. Decide which fits Primer before pushing. Form alt: https://forms.gle/UXQFCogLYrPFvfoUA"

fork_clone "tensorchord/Awesome-LLMOps" \
  "README.md (LLMOps/application table)" \
  "insert the table ROW alphabetically near LangChain/LlamaIndex/Haystack; run their star-badge generation script (see contributing.md) before committing."

# --- awesome-selfhosted: NOT a README PR — YAML add in the DATA repo ----------
echo; echo "=== awesome-selfhosted/awesome-selfhosted-data ==="
SH="$ROOT/awesome-selfhosted-data"
if [ -d "$SH" ]; then echo "  already cloned — skipping"; else
  gh repo fork "awesome-selfhosted/awesome-selfhosted-data" --clone -- "$SH"
fi
git -C "$SH" checkout -B "$BR" >/dev/null 2>&1 || true
cp "$HERE/awesome-selfhosted-primer.yml" "$SH/software/primer.yml"
echo "  ✅ wrote software/primer.yml (⚠️ verify tags/ + license against the live repo, then):"
echo "     cd \"$SH\" && git add software/primer.yml && git commit -m 'Add Primer' && git push -u origin $BR"
echo "     gh pr create --repo awesome-selfhosted/awesome-selfhosted-data --title 'Add Primer' --web"

# --- mcp.so: a GitHub issue / web submit, not a PR ----------------------------
echo; echo "=== mcp.so (directory — submit via issue / web form) ==="
echo "  Use the 'Submit' link on https://mcp.so, or open an issue with:"
echo "  Title: Add Primer"
echo "  Body:  Primer - self-hosted control plane for fleets of small, context-optimized"
echo "         agents. Built-in MCP server (exposes the full platform tool surface, incl."
echo "         mounting a knowledge collection into an agent's workspace as a live,"
echo "         editable directory) + MCP client. Apache-2.0. https://github.com/primerhq/primer"

echo; echo "──────────────────────────────────────────────────────────────"
echo "Done. Per repo under $ROOT: open SUBMIT.md, place the entry, then run the"
echo "printed push + 'gh pr create' commands. One PR per list; space them out."
