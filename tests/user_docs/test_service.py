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


class TestNestedFeaturesTree:
    """list_sections emits a two-level tree when a section uses `items`
    (full-slug leaves + {title, overview, children} groups)."""

    def _write_doc(self, tmp_path, rel, slug, title, section):
        _write(
            tmp_path, rel,
            f"---\nslug: {slug}\ntitle: {title}\nsection: {section}\n"
            f"summary: s-{slug}\n---\n## H\nbody\n",
        )

    def test_items_resolve_leaves_and_groups(self, tmp_path):
        manifest = (
            "sections:\n"
            "  - id: features\n"
            "    title: Features\n"
            "    icon: tools\n"
            "    order: 1\n"
            "    items:\n"
            "      - features/agents\n"
            "      - title: Toolsets & Tools\n"
            "        overview: toolsets/overview\n"
            "        children:\n"
            "          - toolsets/toolsets-system\n"
            "          - toolsets/toolsets-external\n"
        )
        (tmp_path / "manifest.yaml").write_text(manifest, encoding="utf-8")
        self._write_doc(
            tmp_path, "features/agents.md", "agents", "Agents", "features",
        )
        self._write_doc(
            tmp_path, "toolsets/overview.md", "toolsets-overview",
            "Toolsets & Tools", "toolsets",
        )
        self._write_doc(
            tmp_path, "toolsets/toolsets-system.md", "toolsets-system",
            "System Toolsets", "toolsets",
        )
        self._write_doc(
            tmp_path, "toolsets/toolsets-external.md", "toolsets-external",
            "External Toolsets", "toolsets",
        )

        svc = UserDocsService(tmp_path)
        svc.reload_index()
        sections = svc.list_sections()

        assert [s["id"] for s in sections] == ["features"]
        items = sections[0]["docs"]
        # First item is the leaf, second is the group.
        assert items[0]["slug"] == "features/agents"
        assert "group" not in items[0]

        group = items[1]
        assert group["group"] is True
        assert group["title"] == "Toolsets & Tools"
        assert group["overview"]["slug"] == "toolsets/overview"
        child_slugs = [c["slug"] for c in group["children"]]
        assert child_slugs == [
            "toolsets/toolsets-system",
            "toolsets/toolsets-external",
        ]

    def test_unresolved_leaves_and_children_are_skipped(self, tmp_path):
        manifest = (
            "sections:\n"
            "  - id: features\n"
            "    title: Features\n"
            "    icon: tools\n"
            "    order: 1\n"
            "    items:\n"
            "      - features/agents\n"
            "      - features/ghost\n"
            "      - title: G\n"
            "        overview: toolsets/missing\n"
            "        children:\n"
            "          - toolsets/toolsets-system\n"
            "          - toolsets/ghost\n"
        )
        (tmp_path / "manifest.yaml").write_text(manifest, encoding="utf-8")
        self._write_doc(
            tmp_path, "features/agents.md", "agents", "Agents", "features",
        )
        self._write_doc(
            tmp_path, "toolsets/toolsets-system.md", "toolsets-system",
            "System Toolsets", "toolsets",
        )

        svc = UserDocsService(tmp_path)
        svc.reload_index()
        items = svc.list_sections()[0]["docs"]

        # The dangling leaf is dropped; the group survives with its
        # missing overview as None and only the resolvable child.
        assert [i.get("slug") for i in items if not i.get("group")] == [
            "features/agents",
        ]
        group = next(i for i in items if i.get("group"))
        assert group["overview"] is None
        assert [c["slug"] for c in group["children"]] == [
            "toolsets/toolsets-system",
        ]


class TestRealManifestResolves:
    """The shipped manifest must resolve every leaf, every group overview,
    and every group child against the real on-disk corpus (no dangling
    slugs)."""

    def test_shipped_manifest_has_no_dangling_slugs(self):
        from pathlib import Path as _Path

        root = _Path(__file__).resolve().parents[2] / "primer" / "user_docs"
        svc = UserDocsService(root)
        svc.reload_index()

        import yaml as _yaml

        manifest = _yaml.safe_load(
            (root / "manifest.yaml").read_text(encoding="utf-8")
        )
        known = {e.slug for e in svc.all_entries()}
        missing: list[str] = []

        for sec in manifest["sections"]:
            sid = sec["id"]
            for basename in sec.get("docs", []) or []:
                full = f"{sid}/{basename}"
                if full not in known:
                    missing.append(full)
            for item in sec.get("items", []) or []:
                if isinstance(item, str):
                    if item not in known:
                        missing.append(item)
                    continue
                overview = item.get("overview")
                if overview and overview not in known:
                    missing.append(overview)
                for child in item.get("children", []) or []:
                    if child not in known:
                        missing.append(child)

        assert not missing, f"manifest references missing docs: {missing}"

    def test_features_section_is_nested(self):
        from pathlib import Path as _Path

        root = _Path(__file__).resolve().parents[2] / "primer" / "user_docs"
        svc = UserDocsService(root)
        svc.reload_index()
        sections = svc.list_sections()

        features = next(s for s in sections if s["id"] == "features")
        groups = [d for d in features["docs"] if d.get("group")]
        titles = {g["title"] for g in groups}
        # Every group has a resolvable overview and at least one child.
        for g in groups:
            assert g["overview"] is not None, g["title"]
            assert g["children"], g["title"]
        assert {
            "Toolsets & Tools", "Semantic Search", "Workspaces",
            "Graphs", "Web", "Channels",
        } <= titles


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
