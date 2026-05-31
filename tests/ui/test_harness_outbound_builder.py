"""Static JSX checks for the outbound builder wizard — Plan B Phase 8 / Spec B §11.2."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "ui" / "components" / "harness_outbound_builder.jsx"
INDEX = ROOT / "ui" / "index.html"
HARNESSES = ROOT / "ui" / "components" / "harnesses.jsx"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_builder_file_exists():
    assert BUILDER.is_file(), "ui/components/harness_outbound_builder.jsx must exist"


def test_builder_is_loaded_from_index():
    src = _src(INDEX)
    assert "harness_outbound_builder.jsx" in src


def test_builder_exports_global():
    src = _src(BUILDER)
    assert "window.HarnessOutboundBuilder" in src


def test_builder_step_labels_present():
    src = _src(BUILDER)
    # The four step labels must all appear so they show in the step indicator.
    assert "Metadata" in src
    assert "Entities" in src
    assert "Templatize" in src
    # "Link" is the short label used in the indicator
    assert "Link" in src


def test_builder_step_metadata_fields():
    src = _src(BUILDER)
    # Step 1 inputs.
    for ident in ("hob-name", "hob-slug", "hob-git-url", "hob-ref", "hob-subpath", "hob-git-token"):
        assert ident in src, ident
    # Slug uniqueness checked via GET /harnesses?slug=
    assert "/harnesses?slug=" in src


def test_builder_step_entities_fetches_all_kinds():
    src = _src(BUILDER)
    for path in ("/agents", "/graphs", "/toolsets", "/collections"):
        assert path in src, path
    # Greyed-out for harness_id != null
    assert "harness_id" in src
    assert "managed" in src


def test_builder_step_templatize_modal_inputs():
    src = _src(BUILDER)
    assert "override_path" in src
    assert "widget" in src
    # Widget choices include the four built-ins
    for w in ("llm-provider-picker", "embedding-provider-picker", "ssp-picker", "cross-encoder-picker"):
        assert w in src, w


def test_builder_step_link_creates_and_builds():
    src = _src(BUILDER)
    # POST /harnesses (with direction=outbound) + POST /build called.
    assert 'apiFetch("POST", "/harnesses"' in src or "apiFetch('POST', '/harnesses'" in src
    assert "/build" in src
    assert '"outbound"' in src or "'outbound'" in src
    # Optional push-now path.
    assert "/push" in src
    assert "pushNow" in src or "push_now" in src or "Push now" in src


def test_builder_polls_until_done():
    src = _src(BUILDER)
    assert "pending_operation" in src


def test_harnesses_list_opens_builder():
    src = _src(HARNESSES)
    # The list wires the Build outbound button into the builder.
    assert "HarnessOutboundBuilder" in src
