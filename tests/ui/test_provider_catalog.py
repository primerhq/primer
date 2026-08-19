"""The unified provider catalog is one standalone-mountable surface.

S4 section 6 plus amendments M11b (rail gains Web Fetch and Artifact
Storage) and M11d (props-only mount contract). Static source checks, the
same technique as tests/ui/test_capabilities_ui.py.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_the_catalog_file_is_registered_in_index_html() -> None:
    assert 'src="components/provider-catalog.jsx"' in _read("index.html")


def test_the_catalog_exports_itself_on_window() -> None:
    src = _read("components/provider-catalog.jsx")
    assert "window.ProviderCatalog = ProviderCatalog;" in src
    assert "window.PROVIDER_CLASSES = PROVIDER_CLASSES;" in src


def test_every_spec_class_is_on_the_rail() -> None:
    """M11b: eleven classes, Web Fetch and Artifact Storage included."""
    src = _read("components/provider-catalog.jsx")
    for label in (
        "LLM",
        "Embedding",
        "Cross-Encoder",
        "Vector Stores",
        "Speech-to-Text",
        "Text-to-Speech",
        "Web Search",
        "Web Fetch",
        "Artifact Storage",
        "Workspaces",
        "Channels",
    ):
        assert f'label: "{label}"' in src, f"class rail is missing {label}"


def test_each_crud_class_names_its_plural_path() -> None:
    src = _read("components/provider-catalog.jsx")
    for plural in (
        "llm_providers",
        "embedding_providers",
        "cross_encoder_providers",
        "stt_providers",
        "tts_providers",
        "web_search_providers",
        "web_fetch_providers",
        "artifact_storage_providers",
    ):
        assert f'plural: "{plural}"' in src, f"no plural wired for {plural}"


def test_the_mount_contract_is_props_only() -> None:
    """M11d: {initialClass?, initialInstanceId?, onNavigate(ref)} and
    nothing else. S8 re-hosts this component as an overlay, so any
    dependence on the console chrome would have to be unpicked there."""
    src = _read("components/provider-catalog.jsx")
    signature = re.search(r"function ProviderCatalog\(\{([^}]*)\}\)", src, re.S)
    assert signature, "ProviderCatalog must destructure its props inline"
    props = {p.strip().split("=")[0].strip() for p in signature.group(1).split(",")}
    props.discard("")
    assert props == {"initialClass", "initialInstanceId", "onNavigate"}


def test_the_catalog_never_reaches_for_console_chrome() -> None:
    src = _read("components/provider-catalog.jsx")
    for forbidden in ("window.location", "useRouter", "ROUTES", "navigate("):
        assert forbidden not in src, (
            f"{forbidden} in provider-catalog.jsx breaks the standalone mount "
            "contract; route through onNavigate instead"
        )


def test_navigation_goes_through_the_onNavigate_callback() -> None:
    src = _read("components/provider-catalog.jsx")
    assert "onNavigate" in src
    assert re.search(r"onNavigate\(\{\s*kind:", src), (
        "onNavigate must be handed a structured ref, not a URL string"
    )


def test_the_console_mounts_the_catalog_at_slash_providers() -> None:
    src = _read("app.jsx")
    assert re.search(r'\n\s*providers:\s*"/providers"', src)
    assert "window.ProviderCatalog" in src


def test_the_shell_can_address_the_catalog() -> None:
    """An overlay the URL cannot name is unreachable, so the catalog has
    to appear in the shell's overlay vocabulary as well as its host."""
    assert '"providers"' in _read("foundation/shell-url.js")
    assert "providers: {" in _read("components/shell/sh-overlay-host.jsx")


def test_the_reused_class_panels_are_referenced_not_reimplemented() -> None:
    """Vector stores, workspaces and channels already have list panels;
    the catalog hosts those components rather than copying them."""
    src = _read("components/provider-catalog.jsx")
    for component in (
        "window.SSPListPage",
        "window.WorkspaceProvidersPage",
        "window.ChannelProvidersPage",
    ):
        assert component in src
