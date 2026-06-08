"""Locate the agent-facing docs directory across all deployment modes.

The docs live at ``docs/agents/`` in the repo and ship into the container
image (the Dockerfile copies ``docs/``). The resolver falls back to the
legacy in-package ``primer/ai_docs`` location so any environment that
ships only the package still works.
"""
from __future__ import annotations

import os
from pathlib import Path


def resolve_ai_docs_dir() -> Path:
    """Return the directory holding the agent-facing markdown docs.

    Resolution order:
    1. ``PRIMER_AI_DOCS_DIR`` env override (absolute or cwd-relative).
    2. ``<repo_root>/docs/agents`` where repo_root is the parent of the
       installed ``primer`` package.
    3. Legacy ``primer/ai_docs`` (fallback, kept until fully retired).
    """
    env = os.getenv("PRIMER_AI_DOCS_DIR")
    if env:
        return Path(env)
    import primer

    pkg = Path(primer.__file__).resolve().parent
    candidate = pkg.parent / "docs" / "agents"
    if candidate.is_dir():
        return candidate
    return pkg / "ai_docs"
