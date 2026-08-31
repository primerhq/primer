"""The programme definition of done, as assertions (S9 section 8).

Every item that can be checked from the repo is checked here. The two
that cannot (all four lanes green on a real CI run, and the reference
deployment) are covered by Task 25's commands.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_item_1_deletion_gates_exist() -> None:
    """Per-spec grep-clean gates are present, not just claimed.

    Spec section 8 item 1 names six deletion categories: chat, mounting,
    docling, channel richness, old shells, primectl. Five of them are
    vocabulary the retired map must cover; primectl has its own module.
    Checking the files exist is not enough, because an empty RETIRED map
    would pass that and prove nothing.
    """
    for gate in (
        "tests/docs/test_retired_vocabulary.py",
        "tests/docs/test_primectl_removed.py",
    ):
        assert (REPO / gate).exists(), f"missing deletion gate: {gate}"

    from tests.docs.test_retired_vocabulary import RETIRED

    # Patterns spell case classes like [Dd]ocling, so search the whole map,
    # keys and reasons together, for the plain category word.
    joined = " ".join(RETIRED) + " " + " ".join(RETIRED.values())
    for category in ("chat", "mounting", "docling", "studio2", "classic console"):
        assert category in joined, (
            f"the retired-vocabulary map has no guard for {category!r}"
        )


def test_item_1_the_carved_out_chat_engine_is_gone() -> None:
    """The chat CODE is deleted, not just the chat vocabulary.

    S1 P7 carved the headless chat engine out rather than deleting it;
    S6 P5 took the CHAT claim lane and the engine. Without this the
    checklist would pass on a tree that still ships primer/chat,
    ClaimKind.CHAT and run_engine_chat in v2.0.0, because the item above
    only inspects a vocabulary map.
    """
    for gone in (
        "primer/chat",
        "primer/model/chats.py",
        "primer/claim/adapters/chats.py",
        "tests/chat",
    ):
        assert not (REPO / gone).exists(), f"carved-out chat code survives: {gone}"

    from primer.int.claim import ClaimKind
    from primer.worker import engine_handlers

    assert not hasattr(ClaimKind, "CHAT"), "the CHAT claim lane survives"
    assert not hasattr(engine_handlers, "run_engine_chat")


_FACADES = ("_shell_helpers", "_studio_helpers")


def test_item_2_ui_e2e_journeys_go_through_the_shell_facade() -> None:
    """Crosscheck M16: no ui_e2e module reaches around the facade.

    The facade is two modules by S8's design: `_shell_helpers` holds the
    fresh shell's selectors, and `_studio_helpers` survives as a thin
    delegation so smoke tests written between S1 and S7 never needed a
    second rewrite. A test module may import either; what it may not do
    is navigate on its own.

    A goto that never mentions `console_url` is not console navigation
    (the docs-embed spike loads a static page), so the rule binds to
    modules that actually drive the console.
    """
    facade = REPO / "tests" / "ui_e2e" / "_shell_helpers.py"
    assert facade.exists(), "the shell facade helper layer is missing"

    strays: list[str] = []
    for path in (REPO / "tests" / "ui_e2e").rglob("test_*.py"):
        text = path.read_text(encoding="utf-8")
        if "page.goto(" not in text or "console_url" not in text:
            continue
        if not any(f in text for f in _FACADES):
            strays.append(str(path.relative_to(REPO)))
    assert not strays, f"ui_e2e modules bypassing the facade: {strays}"


def test_item_3_packaging_sweep_gate_exists() -> None:
    assert (REPO / "tests" / "docs" / "test_extras_matrix.py").exists()
    assert (REPO / "tests" / "docs" / "test_import_contracts.py").exists()


def test_item_4_agents_md_updated() -> None:
    assert (REPO / "tests" / "docs" / "test_definition_of_done.py").exists()
    assert "primectl" not in (REPO / "AGENTS.md").read_text(encoding="utf-8")


def test_item_5_banner_and_version_agree() -> None:
    """Delegates to the banner gate so the checklist cannot disagree."""
    from tests.docs.test_transition_banner import (
        test_transition_versions_stay_below_v2,
    )

    test_transition_versions_stay_below_v2()


def test_item_6_reference_deployment_is_in_iac() -> None:
    """The 2026-08-14 audit item: limits codified, postgres in IaC.

    The harness repo is a sibling checkout; skip when it is absent so the
    gate stays runnable in a bare clone.
    """
    import pytest
    import yaml

    k8s = REPO.parent / "harness" / "k8s"
    if not k8s.exists():
        pytest.skip("harness checkout not present")

    for name in ("primer-api.yaml", "primer-worker.yaml"):
        docs = list(yaml.safe_load_all((k8s / name).read_text(encoding="utf-8")))
        deployment = next(d for d in docs if d and d["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert "resources" in container, f"{name} declares no resources block"
        assert "limits" in container["resources"], f"{name} declares no limits"

    assert (k8s / "postgres.yaml").exists(), (
        "the postgres Deployment is still outside IaC; a re-apply would "
        "strip the live workload"
    )
