"""primectl is gone from the test tree (S9 section 5, first half).

The repo-wide grep-clean gate is Task 14; this one is scoped to tests so
the deletion can land and be proven before the package itself goes.

`COOKBOOK_RECIPES` is the port-then-delete proof. Fifteen of the CLI
cookbook modules had a same-stem API sibling; two did not, and those two
are the trap this map exists to close: their siblings are real but named
differently, and a reader skimming filenames would conclude the recipe
had no API coverage and either delete it or rewrite it needlessly.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"

# CLI cookbook module stem -> the API-driven module that outlives it.
COOKBOOK_RECIPES: dict[str, str] = {
    "app_builder": "test_cookbook_app_builder.py",
    "code_interpreter": "test_cookbook_code_interpreter.py",
    "compliance_sweep": "test_cookbook_compliance_sweep.py",
    "fanout_code_review": "test_cookbook_fanout_code_review.py",
    "harness_packaging": "test_cookbook_harness_packaging.py",
    "incident_responder": "test_cookbook_incident_responder.py",
    # Named differently: the CLI module's docstring calls this its sibling.
    "iterative_web_research": "test_cookbook_web_research_loop.py",
    "mcp_service": "test_cookbook_mcp_service.py",
    "meta_agent_builder": "test_cookbook_meta_agent_builder.py",
    "onboarding_assembly": "test_cookbook_onboarding_assembly.py",
    "policy_desk": "test_cookbook_policy_desk.py",
    "rag_knowledge_base": "test_cookbook_rag_knowledge_base.py",
    "release_conductor": "test_cookbook_release_conductor.py",
    # Named differently: see the CLI module's docstring.
    "scheduled_stock_monitor": "test_cookbook_stock_monitor.py",
    "skills_loop": "test_cookbook_skills_loop.py",
    "support_desk": "test_cookbook_support_desk.py",
    "tiered_help_desk": "test_cookbook_tiered_help_desk.py",
}


def test_every_cookbook_recipe_keeps_an_api_regression() -> None:
    """No recipe loses its e2e regression when the CLI modules go."""
    missing = [
        f"{stem} -> {api}"
        for stem, api in COOKBOOK_RECIPES.items()
        if not (TESTS / "e2e" / api).exists()
    ]
    assert not missing, f"cookbook recipes with no surviving API test: {missing}"


def test_no_cli_driver_remains() -> None:
    assert not (TESTS / "_support" / "primectl_driver.py").exists()
    assert not (TESTS / "primectl").exists()
    assert not (TESTS / "test_primectl_version.py").exists()


def test_no_cli_cookbook_siblings_remain() -> None:
    strays = sorted(p.name for p in (TESTS / "e2e").glob("test_cookbook_*_cli.py"))
    assert not strays, f"CLI cookbook e2e still present: {strays}"


def test_primer_cli_tests_are_kept() -> None:
    """tests/cli covers `primer init` on primer.cli, not the removed CLI."""
    init_test = TESTS / "cli" / "test_init_command.py"
    assert init_test.exists()
    assert "import primer.cli" in init_test.read_text(encoding="utf-8")
