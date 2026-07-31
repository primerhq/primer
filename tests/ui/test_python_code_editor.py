"""The CodeMirror-backed python editing surface.

Static-source + bundle-build checks, matching the rest of tests/ui/. The
behaviour that needs a browser (mounting, completion, lint marks) is in
tests/ui_e2e/test_python_builder.py.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CODE_EDITOR = UI / "components" / "toolsets" / "python-code-editor.jsx"
EDITOR = UI / "components" / "toolsets" / "python-editor.jsx"
STYLES = UI / "styles.css"
INDEX = UI / "index.html"
MANIFEST = UI / "vendor" / "MANIFEST.md"
BUNDLE = UI / "vendor" / "codemirror.min.js"


def _src() -> str:
    return CODE_EDITOR.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The vendored bundle
# ---------------------------------------------------------------------------


def test_the_bundle_is_vendored_and_catalogued() -> None:
    # ui/vendor/MANIFEST.md is the operator's supply-chain audit trail; a file
    # that is not in it is exactly what that document exists to prevent.
    assert BUNDLE.exists()
    manifest = MANIFEST.read_text(encoding="utf-8")
    assert "codemirror.min.js" in manifest
    assert "codemirror.entry.js" in manifest


def test_the_manifest_records_the_sha_of_the_actual_file() -> None:
    import hashlib

    digest = hashlib.sha256(BUNDLE.read_bytes()).hexdigest()
    assert digest in MANIFEST.read_text(encoding="utf-8"), (
        "codemirror.min.js changed without its manifest sha256 being updated"
    )


def test_the_manifest_lists_every_bundled_package() -> None:
    # This is the one vendored file with transitive packages baked in, so the
    # inventory has to be reviewable as a list rather than implied.
    manifest = MANIFEST.read_text(encoding="utf-8")
    for pkg in ("@codemirror/state", "@codemirror/view", "@codemirror/lang-python",
                "@lezer/python", "style-mod", "w3c-keyname"):
        assert pkg in manifest, pkg


def test_the_manifest_carries_a_rebuild_recipe() -> None:
    # "no auto-updates" only means something if the next person can reproduce
    # the artifact.
    manifest = MANIFEST.read_text(encoding="utf-8")
    assert "esbuild" in manifest
    assert "--format=iife" in manifest
    assert "codemirror.entry.js" in manifest


def test_the_bundle_loads_before_the_components() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert "vendor/codemirror.min.js" in html
    assert html.index("vendor/codemirror.min.js") < html.index(
        "components/toolsets/python-code-editor.jsx"
    )


def test_the_entry_is_not_served_to_the_browser() -> None:
    # It is vendored for provenance only. Shipping it would be a second,
    # unbundled copy of the import graph -- and it is ESM, so the browser
    # would fail on it anyway.
    #
    # Matched as a script SOURCE, not as a substring: the comment above the
    # real script tag names the entry file deliberately, and a bare
    # `not in html` would forbid documenting it.
    html = INDEX.read_text(encoding="utf-8")
    assert not re.search(r'<script[^>]*src=["\'][^"\']*codemirror\.entry\.js', html)


def test_the_editor_is_registered_before_its_consumer() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert html.index("components/toolsets/python-code-editor.jsx") < html.index(
        "components/toolsets/python-editor.jsx"
    )


# ---------------------------------------------------------------------------
# Exports + surface
# ---------------------------------------------------------------------------


def test_it_exports_what_the_page_uses() -> None:
    src = _src()
    for name in ("PY_CodeEditor", "PY_SCAFFOLDS", "PY_completionSource",
                 "PY_revealLine", "PY_appendSource"):
        assert f"window.{name}" in src, name


def test_only_one_global_is_taken_from_the_vendored_bundle() -> None:
    # The entry exposes a single curated object; reaching past it would make
    # the vendored surface unauditable.
    src = _src()
    assert "window.CM6" in src
    assert not re.search(r"window\.(CodeMirror|EditorView|EditorState)\b", src)


def test_it_degrades_to_a_textarea_without_the_bundle() -> None:
    # 447KB that failed to load must not mean an operator cannot fix a broken
    # tool.
    src = _src()
    assert 'data-editor="fallback"' in src
    assert "<textarea" in src


# ---------------------------------------------------------------------------
# Completions -- primer's surface, which is the part no Python docs cover
# ---------------------------------------------------------------------------


def test_completions_cover_every_injected_name() -> None:
    src = _src()
    for name in ("primer_tool", "resumes", "ask_user", "sleep_for",
                 "watch_files", "ctx"):
        assert name in src, name


def test_completions_cover_the_docstring_sections() -> None:
    src = _src()
    for section in ("Use when", "Args:", "Examples:"):
        assert section in src, section


def test_every_completion_explains_itself() -> None:
    # The list is the only place this surface is discoverable, so a bare
    # label would be a missed opportunity rather than a completion.
    src = _src()
    api_block = src[src.index("PY_API_COMPLETIONS = ["):src.index("PY_DOC_COMPLETIONS")]
    labels = re.findall(r"label:", api_block)
    infos = re.findall(r"info:", api_block)
    assert len(labels) == len(infos), "every completion needs an info string"
    assert len(labels) >= 6


# ---------------------------------------------------------------------------
# Scaffolds
# ---------------------------------------------------------------------------


def test_both_tool_shapes_have_a_scaffold() -> None:
    src = _src()
    assert "PY_SCAFFOLD_TOOL" in src
    assert "PY_SCAFFOLD_YIELDING" in src


def test_the_scaffolds_state_the_contract_in_comments() -> None:
    # The whole point of scaffolding over an empty buffer: the rules are
    # stated where they apply, as ordinary Python comments the operator can
    # delete once learned.
    src = _src()
    for phrase in ("timeout_seconds", "Use when", "Args:",
                   "fails registration", "ctx"):
        assert phrase in src, phrase


def test_the_yielding_scaffold_includes_its_companion() -> None:
    # A yielding tool without @resumes does not work, so the scaffold must
    # not teach half of it.
    src = _src()
    block = src[src.index("PY_SCAFFOLD_YIELDING"):src.index("PY_SCAFFOLDS")]
    assert "@resumes(" in block
    assert "ask_user(" in block
    assert "ctx" in block


# ---------------------------------------------------------------------------
# Theme tokens
# ---------------------------------------------------------------------------


def test_every_css_token_the_editor_uses_is_defined() -> None:
    # This repo has shipped an undefined token before (--fs-14). An undefined
    # var silently falls back to the browser default, which for an editor
    # means a white box in a dark console.
    css = STYLES.read_text(encoding="utf-8")
    defined = set(re.findall(r"(--[a-zA-Z0-9-]+)\s*:", css))
    used = set()
    for path in (CODE_EDITOR, EDITOR):
        used |= set(re.findall(r"var\((--[a-zA-Z0-9-]+)",
                               path.read_text(encoding="utf-8")))
    missing = sorted(used - defined)
    assert not missing, f"undefined CSS tokens: {missing}"


def test_the_theme_follows_the_console_rather_than_hardcoding_dark() -> None:
    src = _src()
    assert "PY_isDarkTheme" in src
    assert "data-theme" in src


def test_icons_used_by_the_builder_exist() -> None:
    # Icon falls through to a generic circle for an unknown name, so a typo
    # renders something meaningless rather than failing.
    shared = (UI / "components" / "shared.jsx").read_text(encoding="utf-8")
    known = set(re.findall(r'case "([a-z0-9-]+)":', shared))
    used = set(re.findall(r'<Icon name="([a-z0-9-]+)"',
                          EDITOR.read_text(encoding="utf-8")))
    assert used, "expected the builder to render some icons"
    assert used <= known, f"unknown icon names: {sorted(used - known)}"


def test_the_bundle_builds_with_the_code_editor() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    _etag, body = build_jsx_bundle(UI)
    text = body.decode("utf-8")
    assert "window.PY_CodeEditor" in text
    assert "window.PY_SCAFFOLDS" in text
