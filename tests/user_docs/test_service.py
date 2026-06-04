"""Doc service: walks the source tree, builds the index, hot-reloads."""

from __future__ import annotations

import os
from pathlib import Path

from primer.user_docs_service import DocEntry, UserDocsService


def _write(tmp_path: Path, rel: str, body: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _manifest(tmp_path: Path, doc_map: dict[str, list[str]]) -> Path:
    """Write a minimal manifest.yaml listing the given docs per section."""
    lines = ["sections:"]
    order = 1
    for section_id, docs in doc_map.items():
        lines.append(f"  - id: {section_id}")
        lines.append(f"    title: {section_id.title()}")
        lines.append("    icon: doc")
        lines.append(f"    order: {order}")
        if docs:
            lines.append("    docs:")
            for d in docs:
                lines.append(f"      - {d}")
        else:
            lines.append("    docs: []")
        order += 1
    p = tmp_path / "manifest.yaml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


class TestWalkAndIndex:
    def test_walks_tree_and_indexes_docs(self, tmp_path):
        _manifest(tmp_path, {"features": ["agents"]})
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: Agents\nsection: features\n"
            "summary: x\n---\n## Overview\nbody\n",
        )

        svc = UserDocsService(tmp_path)
        svc.reload_index()

        entry = svc.get_doc("features/agents")
        assert isinstance(entry, DocEntry)
        assert entry.slug == "features/agents"
        assert entry.frontmatter["title"] == "Agents"
        assert entry.section == "features"
        assert any(h["text"] == "Overview" for h in entry.headings)

    def test_unknown_slug_returns_none(self, tmp_path):
        _manifest(tmp_path, {"features": []})
        svc = UserDocsService(tmp_path)
        svc.reload_index()
        assert svc.get_doc("features/nope") is None


class TestSectionListing:
    def test_list_sections_joins_manifest_with_doc_metadata(self, tmp_path):
        _manifest(tmp_path, {"features": ["agents"], "cookbook": []})
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: Agents\nsection: features\n"
            "summary: how to define agents\n---\nbody\n",
        )

        svc = UserDocsService(tmp_path)
        svc.reload_index()

        sections = svc.list_sections()
        ids = [s["id"] for s in sections]
        assert ids == ["features", "cookbook"]

        features = sections[0]
        assert features["title"] == "Features"
        assert len(features["docs"]) == 1
        doc = features["docs"][0]
        assert doc["slug"] == "features/agents"
        assert doc["title"] == "Agents"
        assert doc["summary"] == "how to define agents"

    def test_section_with_empty_docs_is_present_but_empty(self, tmp_path):
        _manifest(tmp_path, {"cookbook": []})
        svc = UserDocsService(tmp_path)
        svc.reload_index()
        sections = svc.list_sections()
        assert sections[0]["id"] == "cookbook"
        assert sections[0]["docs"] == []


class TestHeadingExtraction:
    def test_extracts_h2_and_h3_in_order(self, tmp_path):
        _manifest(tmp_path, {"features": ["agents"]})
        _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: Agents\nsection: features\n"
            "summary: x\n---\n## Overview\ntext\n### Sub-section\nx\n"
            "## Lifecycle\ny\n### Approval\nz\n",
        )
        svc = UserDocsService(tmp_path)
        svc.reload_index()
        entry = svc.get_doc("features/agents")
        assert entry.headings == [
            {"level": 2, "text": "Overview", "anchor": "overview"},
            {"level": 3, "text": "Sub-section", "anchor": "sub-section"},
            {"level": 2, "text": "Lifecycle", "anchor": "lifecycle"},
            {"level": 3, "text": "Approval", "anchor": "approval"},
        ]


class TestHotReload:
    def test_get_doc_rereads_when_mtime_advances(self, tmp_path):
        _manifest(tmp_path, {"features": ["agents"]})
        path = _write(
            tmp_path, "features/agents.md",
            "---\nslug: agents\ntitle: Agents\nsection: features\n"
            "summary: v1\n---\nbody v1\n",
        )
        svc = UserDocsService(tmp_path)
        svc.reload_index()
        first = svc.get_doc("features/agents")
        assert first.frontmatter["summary"] == "v1"

        new_mtime = path.stat().st_mtime + 5
        path.write_text(
            "---\nslug: agents\ntitle: Agents\nsection: features\n"
            "summary: v2\n---\nbody v2\n",
            encoding="utf-8",
        )
        os.utime(path, (new_mtime, new_mtime))

        second = svc.get_doc("features/agents")
        assert second.frontmatter["summary"] == "v2"
        assert second.body.strip() == "body v2"
