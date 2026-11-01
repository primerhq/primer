"""T123 (GI-16, GI-18): a config toggle within one build rewrites the wiki again; the provider and profile kinds converge the live map; a public workspace
never names a private template or provider; and the agent docs and the wiki ship in the wheel."""

from __future__ import annotations

import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

import primer
from primer.ai_docs_path import resolve_ai_docs_dir
from primer.bootstrap.seed import SYSTEM_CDC_SUBSCRIPTION, ensure_system_event_subscriptions
from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID, converge_entity, regenerate_system_collection
from primer.knowledge.tree import DocumentTreeService
from primer.knowledge.wiki.pages import resolve_wiki_dir
from primer.knowledge.wiki.stamp import decide
from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.event import EventSubscription
from primer.model.except_ import NotFoundError
from primer.model.model_profile import ModelProfile
from primer.model.provider import AnthropicConfig, Limits, LLMProvider, LLMProviderType
from primer.model.wiki import WikiStamp
from primer.model.workspace import (
    LocalWorkspaceConfig, Workspace, WorkspaceProvider, WorkspaceProviderType, WorkspaceRuntimeMeta, WorkspaceTemplate,
)
from tests.knowledge.test_system_collection import sp  # noqa: F401


def _stamp(*, version="1.0.0", content="H2", superseded=("H1",), source="S") -> WikiStamp:
    return WikiStamp(id="system-wiki", app_version=version, content_hash=content, superseded=list(superseded), source_hash=source,
                     written_at=datetime(2026, 1, 1, tzinfo=UTC))


# --- GI-16: the stamp ---------------------------------------------------------------------------------------------------------------------------

def test_a_superseded_hash_may_write_again_within_one_build():
    assert decide(_stamp(), app_version="1.0.0", content_hash="H1", source_hash="S") == "write"


def test_the_rolling_deploy_rules_still_refuse():
    assert decide(_stamp(version="1.1.0"), app_version="1.0.0", content_hash="H1", source_hash="S") == "older"      # an older release
    assert decide(_stamp(), app_version="1.0.0", content_hash="H1", source_hash="OTHER") == "superseded"           # another source: an old pod
    assert decide(_stamp(version="1.0.0+b"), app_version="1.0.0+a", content_hash="H1", source_hash="S") == "superseded"
    assert decide(_stamp(source=None), app_version="1.0.0", content_hash="H1", source_hash="S") == "superseded"   # a stamp from before the field
    assert decide(_stamp(), app_version="1.0.0", content_hash="H2", source_hash="S") == "same"
    assert decide(_stamp(), app_version="1.0.0", content_hash="H3", source_hash="S") == "write"


async def test_canvas_tools_off_then_on_again_brings_the_canvas_pages_back(sp) -> None:  # noqa: F811
    from primer.api.registries import ProviderRegistry
    from primer.bootstrap.seed import builtin_toolset_providers
    from primer.canvas.settings import write_canvas_settings
    from primer.model.canvas import CANVAS_SETTINGS_ID, CanvasSettings
    from tests.docs._live_toolsets import built_in_providers

    reg = ProviderRegistry(
        sp, llm_factory=lambda p: object(), embedder_factory=lambda p: object(), cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(), **{f"{tid}_toolset_provider": p for tid, p in built_in_providers().items()},
    )

    async def canvas_pages() -> list[str]:
        nodes = await DocumentTreeService(sp).tree(collection_id=SYSTEM_COLLECTION_ID, parent="", depth=10)
        return sorted(n.path for n in nodes if n.path.startswith("reference/tools/canvas"))

    async def build() -> None:
        await regenerate_system_collection(sp, toolset_providers=await builtin_toolset_providers(reg), app_version="9.0.0")

    await build()
    assert await canvas_pages()
    await write_canvas_settings(sp, CanvasSettings(id=CANVAS_SETTINGS_ID, agent_tools_enabled=False))
    await build()
    assert not await canvas_pages()
    await write_canvas_settings(sp, CanvasSettings(id=CANVAS_SETTINGS_ID, agent_tools_enabled=True))
    await build()
    assert await canvas_pages()


# --- GI-16: the live map -----------------------------------------------------------------------------------------------------------------------

def _llm(access: Access) -> LLMProvider:
    return LLMProvider(id="prov", provider=LLMProviderType.ANTHROPIC, config=AnthropicConfig(api_key=SecretStr("k")),
                       limits=Limits(max_concurrency=1), access=access)


async def _read(sp, path: str) -> str:  # noqa: F811
    return (await DocumentTreeService(sp).read(collection_id=SYSTEM_COLLECTION_ID, path=path)).body


async def test_the_cdc_subscription_listens_to_the_provider_kinds(sp) -> None:  # noqa: F811
    await ensure_system_event_subscriptions(sp)
    types = set((await sp.get_storage(EventSubscription).get(SYSTEM_CDC_SUBSCRIPTION)).filter.event_types)
    for kind in ("llm_provider", "model_profile", "embedding_provider", "semantic_search_provider"):
        assert f"{kind}.*" in types, kind


def test_the_provider_kinds_emit_events():
    import primer.api.routers.providers  # noqa: F401 - registers the kinds
    from primer.events.registry import kind_for_model
    from primer.model.provider import EmbeddingProvider, SemanticSearchProvider

    for model, kind in ((LLMProvider, "llm_provider"), (ModelProfile, "model_profile"), (EmbeddingProvider, "embedding_provider"),
                        (SemanticSearchProvider, "semantic_search_provider")):
        assert kind_for_model(model) == kind


async def test_a_provider_made_private_leaves_the_live_map_without_a_restart(sp) -> None:  # noqa: F811
    store = sp.get_storage(LLMProvider)
    await store.create(_llm(Access(owners=["u1"], public=True)))
    await regenerate_system_collection(sp, toolset_providers={})
    assert "[prov]" in await _read(sp, "this-install/providers/llm")

    row = await store.get("prov")
    await store.update(row.model_copy(update={"access": Access(owners=["u1"])}))
    assert await converge_entity(sp, entity_type="llm_provider", entity_id="prov") is True

    assert "[prov]" not in await _read(sp, "this-install/providers/llm")
    with pytest.raises(NotFoundError):
        await _read(sp, "this-install/providers/llm/prov")


async def test_a_profile_made_private_changes_the_agent_pages_that_name_it(sp) -> None:  # noqa: F811
    await sp.get_storage(LLMProvider).create(_llm(Access(owners=["u1"], public=True)))
    profiles = sp.get_storage(ModelProfile)
    await profiles.create(ModelProfile(id="prof", description="d", provider_id="prov", model_name="m", context_length=1000,
                                       access=Access(owners=["u1"], public=True)))
    from primer.bootstrap.operator_defaults import operator_agent

    await sp.get_storage(Agent).create(operator_agent("prof").model_copy(update={"access": Access(public=True)}))
    await regenerate_system_collection(sp, toolset_providers={})
    page = "this-install/agents/operator"
    assert "`prof`" in await _read(sp, page)

    row = await profiles.get("prof")
    await profiles.update(row.model_copy(update={"access": Access(owners=["u1"])}))
    await converge_entity(sp, entity_type="model_profile", entity_id="prof")

    assert "a private profile" in await _read(sp, page) and "`prof`" not in await _read(sp, page)


# --- GI-16: a workspace page --------------------------------------------------------------------------------------------------------------------

def _workspace() -> Workspace:
    return Workspace(
        id="ws-pub", name="Public one", template_id="tpl-secret", provider_id="prov-secret", created_at=datetime(2026, 1, 1, tzinfo=UTC),
        phase="running", runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token="t"), access=Access(owners=["u1"], public=True),
    )


async def _world(sp, *, public: bool) -> None:  # noqa: F811
    access = Access(owners=["u1"], public=public)
    await sp.get_storage(WorkspaceProvider).create(
        WorkspaceProvider(id="prov-secret", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/tmp/primer-t123"), access=access)
    )
    await sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(id="tpl-secret", description="d", provider_id="prov-secret", access=access))
    await sp.get_storage(Workspace).create(_workspace())


@pytest.mark.parametrize("path", ["this-install/workspaces/ws-pub", "this-install/workspaces"])
async def test_a_public_workspace_over_a_private_template_and_provider_names_neither(sp, path) -> None:  # noqa: F811
    await _world(sp, public=False)
    await regenerate_system_collection(sp, toolset_providers={})
    body = await _read(sp, path)
    assert "tpl-secret" not in body and "prov-secret" not in body
    if path.endswith("ws-pub"):
        assert "a private template" in body and "a private provider" in body


async def test_the_converged_workspace_page_hides_them_too(sp) -> None:  # noqa: F811
    await _world(sp, public=False)
    await regenerate_system_collection(sp, toolset_providers={})
    await converge_entity(sp, entity_type="workspace", entity_id="ws-pub")
    assert "tpl-secret" not in await _read(sp, "this-install/workspaces/ws-pub")


async def test_a_public_template_and_provider_are_named(sp) -> None:  # noqa: F811
    await _world(sp, public=True)
    await regenerate_system_collection(sp, toolset_providers={})
    body = await _read(sp, "this-install/workspaces/ws-pub")
    assert "tpl-secret" in body and "prov-secret" in body


# --- GR-9: docs in the wheel --------------------------------------------------------------------------------------------------------------------

def test_the_wheel_force_includes_the_agent_docs_and_the_wiki():
    root = Path(__file__).resolve().parents[2]
    wheel = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]["wheel"]
    include = wheel["force-include"]
    assert include["docs/agents"] == "primer/_docs/agents" and include["docs/wiki"] == "primer/_docs/wiki"
    assert not (Path(primer.__file__).parent / "ai_docs").exists()


def test_the_resolvers_find_the_docs_in_a_pip_layout(tmp_path, monkeypatch):
    pkg = tmp_path / "site-packages" / "primer"
    (pkg / "_docs" / "agents").mkdir(parents=True)
    (pkg / "_docs" / "wiki").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "_docs" / "agents" / "a.md").write_text("# a")
    monkeypatch.delenv("PRIMER_AI_DOCS_DIR", raising=False)
    monkeypatch.delenv("PRIMER_WIKI_DIR", raising=False)
    monkeypatch.setattr(primer, "__file__", str(pkg / "__init__.py"))
    assert resolve_ai_docs_dir() == pkg / "_docs" / "agents" and resolve_wiki_dir() == pkg / "_docs" / "wiki"
    assert [p.name for p in resolve_ai_docs_dir().rglob("*.md")] == ["a.md"]


def test_a_repo_checkout_and_the_env_override_resolve_as_before(tmp_path, monkeypatch):
    monkeypatch.delenv("PRIMER_AI_DOCS_DIR", raising=False)
    monkeypatch.delenv("PRIMER_WIKI_DIR", raising=False)
    root = Path(primer.__file__).resolve().parent.parent
    assert resolve_ai_docs_dir() == root / "docs" / "agents" and resolve_wiki_dir() == root / "docs" / "wiki"
    monkeypatch.setenv("PRIMER_AI_DOCS_DIR", str(tmp_path))
    assert resolve_ai_docs_dir() == tmp_path
