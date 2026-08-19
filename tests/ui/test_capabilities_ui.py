"""Static checks for capability-aware console states (modular-monolith spec)."""

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_foundation_module_and_script_tag() -> None:
    src = _read("foundation/capabilities.js")
    assert "ns.useCapabilities" in src
    assert "ns.CapabilityGate" in src
    assert "ns.capabilityHint" in src
    assert "ns.EXTRA_FOR_PROVIDER_TYPE" in src
    # apiFetch is (method, path, body, opts); a single-argument call would
    # send the path AS the method to an undefined url.
    assert 'apiFetch("GET", "/capabilities"' in src
    assert "primer-ai[" in src  # hint carries the pip command
    assert 'src="foundation/capabilities.js"' in _read("index.html")


def test_provider_forms_annotate_missing_extras() -> None:
    src = _read("components/provider-form.jsx")
    assert "useCapabilities" in src
    assert "EXTRA_FOR_PROVIDER_TYPE" in src or "capabilityHint" in src


def test_channels_page_gates_on_channels_extra() -> None:
    src = _read("components/channels.jsx")
    assert "useCapabilities" in src or "CapabilityGate" in src


def test_semantic_search_and_knowledge_hint() -> None:
    assert "capabilityHint" in _read("components/semantic-search.jsx")
    assert "capabilityHint" in _read("components/knowledge.jsx")


def test_workspace_provider_form_annotates_backends() -> None:
    # Locate the file that renders the workspace provider type choices.
    hits = [
        p
        for p in (UI / "components").rglob("*.jsx")
        if "kubernetes" in p.read_text(encoding="utf-8")
        and "useCapabilities" in p.read_text(encoding="utf-8")
    ]
    assert hits, "no workspace-provider form consumes useCapabilities yet"


def test_gate_is_permissive_while_loading() -> None:
    """An unknown capability must read as installed.

    The endpoint is a fetch like any other, so a page that treated
    "not yet loaded" as "not installed" would flash a not-installed panel
    on every load before settling.
    """
    src = _read("foundation/capabilities.js")
    assert "return true" in src
