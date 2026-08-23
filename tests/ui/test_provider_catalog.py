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


def test_the_console_mounts_the_catalog() -> None:
    """The overlay host is the console's only mount site."""
    assert "window.ProviderCatalog" in _read("components/console/nv-overlays.jsx")


def test_the_shell_can_address_the_catalog() -> None:
    """An overlay the URL cannot name is unreachable, so the catalog has
    to appear in the shell's overlay vocabulary as well as its host."""
    assert '"providers"' in _read("foundation/shell-url.js")
    assert "providers: {" in _read("components/console/nv-overlays.jsx")


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


def test_every_panel_class_can_reach_its_own_detail_view() -> None:
    """Regression: selecting an instance showed the list again.

    A panel class that declares no detail leaves the catalog with nothing
    to render for one instance, so it fell back to the list: a vector
    store's own page, and a workspace provider's, were unreachable even
    though both components are defined, exported and loaded.

    Detail components predate the catalog and name their id after the
    thing they show, so a class says which prop carries it rather than
    every component being renamed to suit the host.
    """
    src = _read("components/provider-catalog.jsx")
    classes = src[:src.index("function PC_ClassRail")]
    for key in ("ssp", "workspace", "channel"):
        block = classes[classes.index(f'key: "{key}"'):]
        block = block[:block.index("},")]
        assert "detail:" in block, f"the {key} class has no detail view"
    assert 'detailProp: "sspId"' in classes, (
        "SSPDetail takes sspId, so the class has to say so"
    )
    assert 'detailProps[cls.detailProp || "providerId"]' in \
        _read("components/provider-catalog.jsx")


def test_creating_a_provider_refreshes_the_list_beside_the_form() -> None:
    """Regression: a created provider did not appear until you left.

    save() bumped a reloadKey that nothing depends on. The instance list
    hands its refetch up through onRegisterRefetch, and the row action
    beside it already used that; the create simply never called it, so
    the row only showed after navigating away and back.
    """
    src = _read("components/provider-catalog.jsx")
    save = src[src.index("const save = async (body) =>"):]
    save = save[:save.index("const selectInstance")]
    assert "listRefetchRef.current()" in save, (
        "the create has to refresh the list it just added a row to"
    )
    assert "selectInstance(body.id)" in save, (
        "and select what was just made, which is where the operator is "
        "already looking"
    )


def test_the_catalog_follows_the_addressed_instance() -> None:
    """Regression: the vector-store detail never opened.

    initialInstanceId seeded local state once and was never looked at
    again. A page hosted inside the catalog that navigates on its own,
    which the vector-store list does to /ssp/<id>, therefore updated the
    url and the crumb while the catalog went on showing the list: its own
    state had not heard about it. The id slot is what says which instance
    is open, so the catalog has to follow it.
    """
    src = _read("components/provider-catalog.jsx")
    assert "setInstanceId(initialInstanceId || null);" in src
    assert "}, [initialInstanceId]);" in src, (
        "the effect has to depend on the prop it is following"
    )
    assert "}, [initialClass]);" in src, (
        "and the class, addressed the same way in the section slot"
    )
