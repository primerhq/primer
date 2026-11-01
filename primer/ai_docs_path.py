"""Locate the agent-facing docs directory across all deployment modes.

The docs live at ``docs/agents/`` in the repo and ship into the container
image (the Dockerfile copies ``docs/``) and into the wheel, which maps them to
``primer/_docs/agents`` (pyproject ``force-include``), so a pip-installed
``primer-ai`` finds them beside the package.
"""
from __future__ import annotations

import os
from pathlib import Path


def resolve_ai_docs_dir() -> Path:
    """Return the directory holding the agent-facing markdown docs.

    Resolution order:
    1. ``PRIMER_AI_DOCS_DIR`` env override (absolute or cwd-relative).
    2. ``primer/_docs/agents`` inside the installed package (the wheel).
    3. ``<repo_root>/docs/agents`` where repo_root is the parent of the
       ``primer`` package (a checkout, and the Docker image).
    """
    env = os.getenv("PRIMER_AI_DOCS_DIR")
    if env:
        return Path(env)
    import primer

    pkg = Path(primer.__file__).resolve().parent
    packaged = pkg / "_docs" / "agents"
    if packaged.is_dir():
        return packaged
    return pkg.parent / "docs" / "agents"
