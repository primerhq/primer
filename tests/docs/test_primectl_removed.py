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

import subprocess
import tomllib
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


def _pyproject() -> dict:
    with (REPO / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_workspace_member_is_gone() -> None:
    assert not (REPO / "primectl").exists()
    members = _pyproject().get("tool", {}).get("uv", {}).get("workspace", {})
    assert "primectl" not in members.get("members", [])


def test_version_pin_is_gone() -> None:
    pins = _pyproject()["tool"]["semantic_release"]["version_toml"]
    assert all("primectl" not in pin for pin in pins)
    assert "pyproject.toml:project.version" in pins
    assert "runtime/pyproject.toml:project.version" in pins


def test_release_pipeline_has_no_cli_package() -> None:
    release = (REPO / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assert "primectl" not in release


def test_ci_sync_steps_do_not_install_the_cli() -> None:
    for name in ("ci.yml", "e2e.yml"):
        body = (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "./primectl" not in body, f"{name} still installs the CLI"


def test_dependabot_has_no_cli_entry() -> None:
    body = (REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert "primectl" not in body


_SEARCH_ROOTS = ("primer", "ui", "tests", "docs/dev", "docs/agents", "scripts")
_SEARCH_FILES = ("README.md", "AGENTS.md", "CONTRIBUTING.md", "Makefile")
_SUFFIXES = {".py", ".jsx", ".js", ".md", ".yml", ".yaml", ".toml", ".sh"}


def _tracked_files() -> list[Path]:
    """Git-tracked candidates only.

    Walking the filesystem would sweep build artifacts in: the embed
    harness bundle at scripts/docs/embed_harness/_app.js is gitignored,
    is 1.7M of compiled JSX, and carries whatever the console said when
    it was last built. Grepping it makes this gate pass in CI (fresh
    checkout, no bundle) and fail on any developer machine that has run
    the capture harness. `git ls-files` is the repo, which is what
    "repo-wide grep-clean" means.
    """
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", *_SEARCH_ROOTS, *_SEARCH_FILES],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        REPO / rel
        for rel in out.split("\0")
        if rel and (Path(rel).suffix in _SUFFIXES or Path(rel).name == "Makefile")
    ]


def _hits() -> list[str]:
    found: list[str] = []
    for path in _tracked_files():
        # Excluded for the reason this file is: holding the name in
        # order to forbid it is the whole job of each of these gates.
        if not path.exists() or path.name in (
            "test_primectl_removed.py",
            "test_definition_of_done.py",
            "test_cutover_checklist.py",
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "primectl" in line:
                found.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()}")
    return found


def test_no_primectl_reference_survives() -> None:
    hits = _hits()
    assert not hits, "primectl still referenced:\n  " + "\n  ".join(hits)
