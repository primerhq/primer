"""Tests: embed: directive is recognized and validated by the lint engine.

Verifies:
- embed:<registered-id> lints clean.
- embed:<unknown-id> raises unknown_embed_id.
- mockup:<id> is an unknown directive (no longer supported).
"""

from __future__ import annotations

import json
import pathlib
from pathlib import Path

from primer.user_docs_lint import LintIssue, run_lint
from primer.user_docs_service import UserDocsService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, rel: str, body: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _svc(tmp_path: Path) -> UserDocsService:
    (tmp_path / "manifest.yaml").write_text(
        "sections:\n  - id: features\n    title: Features\n"
        "    icon: doc\n    order: 1\n    docs: []\n",
        encoding="utf-8",
    )
    svc = UserDocsService(tmp_path)
    svc.reload_index()
    return svc


def _codes(issues: list[LintIssue]) -> list[str]:
    return [i.rule for i in issues]


# ---------------------------------------------------------------------------
# Build the manifest from registry.json only (mockup ids removed).
# ---------------------------------------------------------------------------

_REGISTRY_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "primer" / "user_docs" / "_fixtures" / "registry.json"
)
try:
    _registry_data = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    _REGISTRY_MANIFEST: list[str] = _registry_data.get("embeds", [])
except Exception:  # noqa: BLE001
    _REGISTRY_MANIFEST = []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEmbedDirective:
    def test_embed_known_id_lints_clean(self, tmp_path):
        """embed:<id> with a registry-registered id must not raise unknown_embed_id."""
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: Agents\nsection: features\n"
            "summary: x\n---\n## Overview\n\n"
            "```embed:agents-page\n```\n",
        )
        svc = _svc(tmp_path)
        issues = run_lint(svc, embeds_manifest=_REGISTRY_MANIFEST)
        assert "unknown_embed_id" not in _codes(issues)

    def test_embed_unknown_id_raises_error(self, tmp_path):
        """embed:<id> with an unregistered id must emit unknown_embed_id."""
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: Agents\nsection: features\n"
            "summary: x\n---\n## Overview\n\n"
            "```embed:not-a-real-id\n```\n",
        )
        svc = _svc(tmp_path)
        issues = run_lint(svc, embeds_manifest=_REGISTRY_MANIFEST)
        bad = [i for i in issues if i.rule == "unknown_embed_id"]
        assert len(bad) == 1
        assert "not-a-real-id" in bad[0].message
        assert bad[0].severity == "error"

    def test_mockup_directive_is_unknown(self, tmp_path):
        """mockup:<id> is no longer a supported directive and must not be
        recognized by the lint (it will be treated as a plain fenced block
        with an unrecognized info-string, not as an embed directive).

        The lint does NOT emit unknown_embed_id for mockup: blocks because
        mockup: is not in _DIRECTIVE_PREFIXES. The block is simply ignored
        by the directive walker -- no lint error is produced for the
        directive itself, but no lints-clean guarantee is provided either.
        """
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: Agents\nsection: features\n"
            "summary: x\n---\n## Overview\n\n"
            "```mockup:agent-create-modal\n```\n",
        )
        svc = _svc(tmp_path)
        issues = run_lint(svc, embeds_manifest=_REGISTRY_MANIFEST)
        # mockup: is not in _DIRECTIVE_PREFIXES so the walker skips it
        # entirely -- no unknown_embed_id fires (the block is invisible to
        # the lint). The important contract: mockup: is NOT validated as an
        # embed, and any doc using it gets no coverage from the embed check.
        # The directive is unsupported; authors must use embed: instead.
        assert "mockup:agent-create-modal" not in str(issues)
        # And no embed-related error fires either
        assert "unknown_embed_id" not in _codes(issues)
