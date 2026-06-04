"""Lint engine tests. One fixture per rule from spec section 10.1."""

from __future__ import annotations

from pathlib import Path

from primer.user_docs_lint import LintIssue, run_lint
from primer.user_docs_service import UserDocsService


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


class TestNoEmDash:
    def test_em_dash_in_body_is_rejected(self, tmp_path):
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: Agents\nsection: features\n"
            "summary: x\n---\nLine with " + "—" + " dash here.\n",
        )
        svc = _svc(tmp_path)
        assert "no_em_dash" in _codes(run_lint(svc, embeds_manifest=[]))

    def test_em_dash_in_frontmatter_is_rejected(self, tmp_path):
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: Agents " + "—" + " def\n"
            "section: features\nsummary: x\n---\nbody\n",
        )
        svc = _svc(tmp_path)
        assert "no_em_dash" in _codes(run_lint(svc, embeds_manifest=[]))

    def test_hyphens_are_fine(self, tmp_path):
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: Agents - definitions\n"
            "section: features\nsummary: see -- here\n---\n"
            "body with hyphens - and -- and --- triple\n",
        )
        svc = _svc(tmp_path)
        assert "no_em_dash" not in _codes(run_lint(svc, embeds_manifest=[]))


class TestSlugUniqueness:
    def test_duplicate_slugs_across_sections_is_rejected(self, tmp_path):
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: A\nsection: features\n"
            "summary: x\n---\nbody\n",
        )
        _write(
            tmp_path, "concepts/agents.md",
            "---\nslug: agents\ntitle: B\nsection: concepts\n"
            "summary: x\n---\nbody\n",
        )
        svc = _svc(tmp_path)
        assert "duplicate_slug" in _codes(run_lint(svc, embeds_manifest=[]))


class TestFileLocationMatchesFrontmatter:
    def test_mismatched_section_is_rejected(self, tmp_path):
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: A\nsection: concepts\n"
            "summary: x\n---\nbody\n",
        )
        svc = _svc(tmp_path)
        assert "section_path_mismatch" in _codes(
            run_lint(svc, embeds_manifest=[])
        )


class TestHeadingDepth:
    def test_h1_in_body_is_rejected(self, tmp_path):
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: A\nsection: features\n"
            "summary: x\n---\n# Top\n## Sub\n",
        )
        svc = _svc(tmp_path)
        assert "h1_in_body" in _codes(run_lint(svc, embeds_manifest=[]))

    def test_h2_only_is_fine(self, tmp_path):
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: A\nsection: features\n"
            "summary: x\n---\n## OK\n### Sub\n",
        )
        svc = _svc(tmp_path)
        assert "h1_in_body" not in _codes(run_lint(svc, embeds_manifest=[]))


class TestForbiddenTokens:
    def test_todo_in_body_is_warning(self, tmp_path):
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: A\nsection: features\n"
            "summary: x\n---\nTODO finish this.\n",
        )
        svc = _svc(tmp_path)
        todos = [i for i in run_lint(svc, embeds_manifest=[])
                 if i.rule == "forbidden_token"]
        assert len(todos) == 1
        assert todos[0].severity == "warning"


class TestFrontmatterRequiredKeys:
    def test_missing_title_is_rejected(self, tmp_path):
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\nsection: features\nsummary: x\n"
            "---\nbody\n",
        )
        svc = _svc(tmp_path)
        bad = [i for i in run_lint(svc, embeds_manifest=[])
               if i.rule == "missing_frontmatter_key"]
        assert any("title" in i.message for i in bad)

    def test_cookbook_missing_difficulty_is_rejected(self, tmp_path):
        _write(
            tmp_path, "cookbook/recipe.md",
            "---\nslug: recipe\ntitle: R\nsection: cookbook\n"
            "summary: x\ntime_minutes: 10\ntags: []\n---\nbody\n",
        )
        (tmp_path / "manifest.yaml").write_text(
            "sections:\n  - id: cookbook\n    title: Cookbook\n"
            "    icon: code\n    order: 1\n    docs: []\n",
            encoding="utf-8",
        )
        svc = UserDocsService(tmp_path)
        svc.reload_index()
        bad = [i for i in run_lint(svc, embeds_manifest=[])
               if i.rule == "missing_frontmatter_key"]
        assert any("difficulty" in i.message for i in bad)


class TestMockupJsonValid:
    def test_malformed_mockup_json_is_rejected(self, tmp_path):
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: A\nsection: features\n"
            "summary: x\n---\n## h\n\n```mockup:agent-create-modal\n"
            "{ this is not valid json }\n```\n",
        )
        svc = _svc(tmp_path)
        assert "mockup_invalid_json" in _codes(
            run_lint(svc, embeds_manifest=["agent-create-modal"])
        )

    def test_empty_mockup_body_is_fine(self, tmp_path):
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: A\nsection: features\n"
            "summary: x\n---\n## h\n\n```mockup:agent-create-modal\n```\n",
        )
        svc = _svc(tmp_path)
        assert "mockup_invalid_json" not in _codes(
            run_lint(svc, embeds_manifest=["agent-create-modal"])
        )


class TestCrossLinkResolution:
    def test_unknown_ref_slug_is_rejected(self, tmp_path):
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: A\nsection: features\n"
            "summary: x\n---\n## h\n\n```ref:features/nope\n```\n",
        )
        svc = _svc(tmp_path)
        assert "broken_ref" in _codes(run_lint(svc, embeds_manifest=[]))

    def test_valid_ref_with_anchor_passes(self, tmp_path):
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: A\nsection: features\n"
            "summary: x\n---\n## Overview\n\n"
            "```ref:features/agents#overview\n```\n",
        )
        svc = _svc(tmp_path)
        assert "broken_ref" not in _codes(run_lint(svc, embeds_manifest=[]))


class TestUnknownMockupId:
    def test_unknown_embed_id_is_rejected(self, tmp_path):
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: A\nsection: features\n"
            "summary: x\n---\n```mockup:not-registered\n```\n",
        )
        svc = _svc(tmp_path)
        bad = [i for i in run_lint(
            svc, embeds_manifest=["agent-create-modal", "topbar"],
        ) if i.rule == "unknown_embed_id"]
        assert len(bad) == 1
        assert "not-registered" in bad[0].message
        assert "agent-create-modal" in bad[0].message


class TestMermaidSyntax:
    def test_unknown_diagram_type_is_rejected(self, tmp_path):
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: A\nsection: features\n"
            "summary: x\n---\n## h\n\n```mermaid\nnotadiagram\n"
            "  gibberish\n```\n",
        )
        svc = _svc(tmp_path)
        assert "mermaid_unknown_type" in _codes(
            run_lint(svc, embeds_manifest=[])
        )

    def test_flowchart_first_line_passes(self, tmp_path):
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: A\nsection: features\n"
            "summary: x\n---\n## h\n\n```mermaid\nflowchart LR\n"
            "  a --> b\n```\n",
        )
        svc = _svc(tmp_path)
        assert "mermaid_unknown_type" not in _codes(
            run_lint(svc, embeds_manifest=[])
        )


class TestMetaExemption:
    def test_meta_doc_is_not_linted(self, tmp_path):
        _write(
            tmp_path, "_meta/authoring-guide.md",
            "---\nslug: authoring-guide\ntitle: Guide\n"
            "section: _meta\nsummary: x\n---\n"
            + "—" + "\n",
        )
        svc = _svc(tmp_path)
        codes = _codes(run_lint(svc, embeds_manifest=[]))
        assert "no_em_dash" not in codes
        assert "reserved_section" not in codes
