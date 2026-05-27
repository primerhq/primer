"""Template render tests — Jinja2 sandboxed."""

from __future__ import annotations

import pytest

from matrix.harness.template import (
    HarnessTemplateError, render_template, RenderedFile, render_bundle,
)


def test_render_simple_substitution():
    out = render_template(
        "name: {{ overrides.x }}",
        overrides={"x": "hi"},
        harness_ctx={"slug": "s"},
    )
    assert out == "name: hi"


def test_render_with_filter():
    out = render_template(
        "name: {{ overrides.x|upper }}",
        overrides={"x": "hi"},
        harness_ctx={"slug": "s"},
    )
    assert out == "name: HI"


def test_render_missing_key_strict():
    with pytest.raises(HarnessTemplateError) as ei:
        render_template(
            "name: {{ overrides.missing }}",
            overrides={},
            harness_ctx={"slug": "s"},
        )
    assert ei.value.code == "template_render_failed"


def test_render_sandbox_blocks_class_access():
    # SandboxedEnvironment blocks unsafe attribute access.
    with pytest.raises(HarnessTemplateError):
        render_template(
            "{{ ''.__class__.__bases__ }}",
            overrides={},
            harness_ctx={"slug": "s"},
        )


def test_render_bundle_walks_templates_dir(tmp_path):
    sub = tmp_path / "sub"
    (sub / "templates").mkdir(parents=True)
    (sub / "templates" / "agent_a.yaml").write_text(
        "kind: agent\nname: a\nspec:\n  description: {{ overrides.x }}\n"
    )
    (sub / "templates" / "graph_a.yaml").write_text(
        "kind: graph\nname: g\nspec: {description: hello}\n"
    )
    result = render_bundle(
        checkout_dir=str(tmp_path),
        subpath="sub",
        overrides={"x": "hello"},
        harness_ctx={"slug": "s"},
    )
    by_name = {f.template_name: f for f in result}
    assert set(by_name) == {"a", "g"}
    assert by_name["a"].kind == "agent"
    assert by_name["a"].rendered["spec"]["description"] == "hello"


def test_render_bundle_rejects_bad_yaml(tmp_path):
    sub = tmp_path / "s"
    (sub / "templates").mkdir(parents=True)
    (sub / "templates" / "bad.yaml").write_text("kind: agent\nname: x\nspec: [unclosed")
    with pytest.raises(HarnessTemplateError) as ei:
        render_bundle(
            checkout_dir=str(tmp_path), subpath="s",
            overrides={}, harness_ctx={"slug": "s"},
        )
    assert ei.value.code == "template_yaml_invalid"


def test_render_bundle_rejects_unknown_kind(tmp_path):
    sub = tmp_path / "s"
    (sub / "templates").mkdir(parents=True)
    (sub / "templates" / "x.yaml").write_text("kind: pickle\nname: x\nspec: {}\n")
    with pytest.raises(HarnessTemplateError) as ei:
        render_bundle(
            checkout_dir=str(tmp_path), subpath="s",
            overrides={}, harness_ctx={"slug": "s"},
        )
    assert ei.value.code == "template_kind_unknown"


def test_render_bundle_reads_content_path(tmp_path):
    sub = tmp_path / "s"
    (sub / "templates").mkdir(parents=True)
    (sub / "files").mkdir()
    (sub / "files" / "doc.md").write_text("# heading\nbody")
    (sub / "templates" / "doc.yaml").write_text(
        "kind: document\nname: d\nspec:\n  collection_id: c\n  name: d\n"
        "  meta: {}\ncontent_path: files/doc.md\n"
    )
    result = render_bundle(
        checkout_dir=str(tmp_path), subpath="s",
        overrides={}, harness_ctx={"slug": "s"},
    )
    assert len(result) == 1
    f = result[0]
    assert f.content == "# heading\nbody"
