"""Tests: embed: directive is recognized and validated by the lint engine.

Verifies:
- embed:<registered-id> lints clean.
- embed:<unknown-id> raises unknown_embed_id.
- mockup:<registered-id> still lints clean (transition: both prefixes valid).
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
# Build the union manifest the same way app.py and docs_lint.py do:
# mockup ids UNION registry.json embed ids.
# ---------------------------------------------------------------------------

_MOCKUP_IDS: list[str] = [
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

_REGISTRY_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "primer" / "user_docs" / "_fixtures" / "registry.json"
)
try:
    _registry_data = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    _registry_ids: list[str] = _registry_data.get("embeds", [])
except Exception:  # noqa: BLE001
    _registry_ids = []

_seen: set[str] = set(_MOCKUP_IDS)
_UNION_MANIFEST: list[str] = list(_MOCKUP_IDS)
for _eid in _registry_ids:
    if _eid not in _seen:
        _UNION_MANIFEST.append(_eid)
        _seen.add(_eid)


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
        issues = run_lint(svc, embeds_manifest=_UNION_MANIFEST)
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
        issues = run_lint(svc, embeds_manifest=_UNION_MANIFEST)
        bad = [i for i in issues if i.rule == "unknown_embed_id"]
        assert len(bad) == 1
        assert "not-a-real-id" in bad[0].message
        assert bad[0].severity == "error"

    def test_mockup_known_id_still_lints_clean(self, tmp_path):
        """mockup:<id> with a currently-valid mockup id must not raise any error
        (transition: both mockup: and embed: are valid side by side)."""
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: Agents\nsection: features\n"
            "summary: x\n---\n## Overview\n\n"
            "```mockup:agent-create-modal\n```\n",
        )
        svc = _svc(tmp_path)
        issues = run_lint(svc, embeds_manifest=_UNION_MANIFEST)
        assert "unknown_embed_id" not in _codes(issues)
