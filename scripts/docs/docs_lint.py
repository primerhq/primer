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
# Embeds manifest -- embed ids from primer/user_docs/_fixtures/registry.json.
# ---------------------------------------------------------------------------
_REGISTRY_PATH = _REPO_ROOT / "primer" / "user_docs" / "_fixtures" / "registry.json"


def load_embeds_manifest(src_root: pathlib.Path | None = None) -> list[str]:
    """Return the registered embed ids from ``_fixtures/registry.json``.

    ``src_root`` defaults to the repo's ``primer/user_docs`` so the CLI
    keeps its existing behaviour; the build passes its own corpus root.
    """
    root = pathlib.Path(src_root) if src_root else _USER_DOCS_ROOT
    registry = root / "_fixtures" / "registry.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
        return list(data.get("embeds", []))
    except Exception:  # noqa: BLE001
        return []


_USER_DOCS_ROOT = _REPO_ROOT / "primer" / "user_docs"

_EMBEDS_MANIFEST: list[str] = load_embeds_manifest()


def index_corpus(src_root: pathlib.Path) -> UserDocsService:
    """Index ``src_root`` into a service, dropping ``_fixtures/`` docs.

    ``reload_index`` walks ``rglob("*.md")`` which would pull in the
    fixture markdown used only to drive embed capture; those are not real
    docs, so we strip them after indexing (matching the CLI's behaviour).
    """
    src_root = pathlib.Path(src_root)
    svc = UserDocsService(src_root)
    svc.reload_index()
    fixtures_prefix = src_root / "_fixtures"
    to_remove = [
        slug for slug, entry in svc._entries.items()  # noqa: SLF001
        if fixtures_prefix in entry.path.parents
        or entry.path.parent == fixtures_prefix
    ]
    for slug in to_remove:
        del svc._entries[slug]  # noqa: SLF001
    return svc


def lint_corpus(src_root: pathlib.Path) -> list:
    """Index ``src_root`` and run every lint rule, returning the issues."""
    svc = index_corpus(src_root)
    return run_lint(svc, embeds_manifest=load_embeds_manifest(src_root))


def main() -> int:
    svc = index_corpus(_USER_DOCS_ROOT)
    issues = run_lint(svc, embeds_manifest=load_embeds_manifest())

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
