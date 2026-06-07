"""Lint the entire user-docs corpus and exit non-zero on any error.

Usage::

    uv run python scripts/docs/docs_lint.py

Loads every ``*.md`` under ``primer/user_docs/`` (excluding
``_fixtures/``), runs the lint with the current embeds manifest, prints
every issue as ``path: rule: message``, and exits 1 on any error.
Exits 0 with a clean-corpus summary when there are no errors.
"""

from __future__ import annotations

import json
import pathlib
import sys

# Resolve repo root (two levels above this script: scripts/docs/ -> repo root)
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from primer.user_docs_lint import run_lint
from primer.user_docs_service import UserDocsService

# ---------------------------------------------------------------------------
# Embeds manifest -- union of the legacy mockup: ids and the new embed: ids
# from primer/user_docs/_fixtures/registry.json. Both remain valid during the
# transition period (Phase 7 will drop the mockup: set). Keep in sync with
# primer/api/app.py _mockup_embed_ids.
# ---------------------------------------------------------------------------
_MOCKUP_EMBED_IDS: list[str] = [
    "topbar",
    "sessions-list-empty",
    "agent-create-modal",
    "graph-canvas-three-nodes",
    "channels-prompt",
    "docs-callout-demo",
    "workspace-empty",
    "session-detail-panel",
    "chat-stream",
    "harness-wizard-step",
    "workspace-template-form",
    "collection-list-empty",
    "ssp-list",
    "trigger-create",
    "worker-stats",
    "api-token-create",
    "bug-reporter-modal",
]

_REGISTRY_PATH = _REPO_ROOT / "primer" / "user_docs" / "_fixtures" / "registry.json"
try:
    _registry_data = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    _registry_embed_ids: list[str] = _registry_data.get("embeds", [])
except Exception:  # noqa: BLE001
    _registry_embed_ids = []

_seen: set[str] = set(_MOCKUP_EMBED_IDS)
_EMBEDS_MANIFEST: list[str] = list(_MOCKUP_EMBED_IDS)
for _eid in _registry_embed_ids:
    if _eid not in _seen:
        _EMBEDS_MANIFEST.append(_eid)
        _seen.add(_eid)

_USER_DOCS_ROOT = _REPO_ROOT / "primer" / "user_docs"


def main() -> int:
    svc = UserDocsService(_USER_DOCS_ROOT)
    # Exclude _fixtures/ by building a patched service that skips that subtree.
    # The simplest approach: reload_index walks rglob("*.md"); we override it
    # to skip paths under _fixtures/ before indexing.
    _fixtures_prefix = _USER_DOCS_ROOT / "_fixtures"
    svc.reload_index()

    # Remove any entries whose path falls under _fixtures/
    to_remove = [
        slug for slug, entry in svc._entries.items()
        if _fixtures_prefix in entry.path.parents or entry.path.parent == _fixtures_prefix
    ]
    for slug in to_remove:
        del svc._entries[slug]

    issues = run_lint(svc, embeds_manifest=_EMBEDS_MANIFEST)

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]

    for issue in sorted(issues, key=lambda i: (i.file, i.line or 0, i.rule)):
        line_part = f":{issue.line}" if issue.line is not None else ""
        sug_part = f" -- {issue.suggestion}" if issue.suggestion else ""
        print(
            f"[{issue.severity}] {issue.file}{line_part}: "
            f"{issue.rule}: {issue.message}{sug_part}"
        )

    n_docs = len(list(svc.all_entries()))
    if errors:
        print(
            f"\nFAIL: {len(errors)} error(s), {len(warnings)} warning(s) "
            f"across {n_docs} doc(s).",
            file=sys.stderr,
        )
        return 1

    if warnings:
        print(
            f"\nOK (with warnings): 0 errors, {len(warnings)} warning(s) "
            f"across {n_docs} doc(s)."
        )
    else:
        print(f"\nOK: corpus of {n_docs} doc(s) lints clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
