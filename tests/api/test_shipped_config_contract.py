"""Shipped config examples must parse into supported runtime settings.

C2 defect: the top-level ``vector_store:`` block shipped in
``config.example.yaml``, the docker entrypoint and the release workflow
was silently dropped by ``AppConfig`` (``extra="ignore"``) -- an operator
selecting pgvector through a shipped example got a "successful" startup
with the setting vanishing at parse time. This probe is the red-first
acceptance test: every top-level key a shipped config emits must be a
field ``AppConfig`` actually reads. A key that vanishes with no runtime
effect is a contract violation.

Covers the brief's "Parsing: every shipped config example (source,
container, release-generated) parses into an observable, supported
runtime setting" and "No release artifact emits a field that AppConfig
silently drops".
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from primer.api.config import AppConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


def _top_level_keys(text: str) -> set[str]:
    """Top-level mapping keys of a YAML config document."""
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict), "shipped config must be a YAML mapping"
    return set(doc.keys())


def _entrypoint_heredocs(text: str) -> list[str]:
    blocks = re.findall(
        r"cat > /app/config\.yaml <<EOF\n(.*?)\nEOF", text, re.S
    )
    assert blocks, "entrypoint heredoc(s) not found"
    # Render ${VAR} / ${VAR:=default} the way the entrypoint's env would.
    return [
        re.sub(r"\$\{(\w+)(?::=([^}]*))?\}", lambda m: m.group(2) or "x", b)
        for b in blocks
    ]


def _release_heredoc(text: str) -> str:
    # The workflow uses a QUOTED heredoc (<<'EOF'); both the body and the
    # terminator are indented inside the run: block.
    blocks = re.findall(r"<<'EOF'\n(.*?)\n\s*EOF\b", text, re.S)
    candidates = [b for b in blocks if "primer.log" in b]
    assert candidates, "release boot-smoke config heredoc not found"
    return candidates[0]


def test_example_config_parses_into_supported_settings():
    """config.example.yaml: every top-level key is an AppConfig field."""
    keys = _top_level_keys((REPO_ROOT / "config.example.yaml").read_text())
    dropped = keys - set(AppConfig.model_fields)
    assert not dropped, (
        f"config.example.yaml emits keys AppConfig silently drops "
        f"(extra=ignore): {sorted(dropped)} -- a shipped example must only "
        f"carry supported settings"
    )


def test_entrypoint_configs_parse_into_supported_settings():
    """docker entrypoint: every rendered config's keys are AppConfig fields."""
    src = (REPO_ROOT / "docker" / "primer" / "entrypoint.sh").read_text()
    for i, rendered in enumerate(_entrypoint_heredocs(src)):
        keys = _top_level_keys(rendered)
        dropped = keys - set(AppConfig.model_fields)
        assert not dropped, (
            f"entrypoint mode {i} renders keys AppConfig silently drops: "
            f"{sorted(dropped)}"
        )


def test_release_config_parses_into_supported_settings():
    """release workflow: the boot-smoke config's keys are AppConfig fields."""
    src = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text()
    rendered = _release_heredoc(src)
    keys = _top_level_keys(rendered)
    dropped = keys - set(AppConfig.model_fields)
    assert not dropped, (
        f"release-generated config emits keys AppConfig silently drops: "
        f"{sorted(dropped)}"
    )


def test_e2e_bringup_emits_no_unsupported_top_level_keys():
    """bringup.sh assembles the e2e server config; it must not emit a
    top-level key AppConfig silently drops either (static check: the
    script appends heredoc fragments, so grep the fragments' first
    column)."""
    src = (REPO_ROOT / "scripts" / "e2e" / "bringup.sh").read_text()
    offenders = [
        line
        for line in src.splitlines()
        if re.match(r"^[A-Za-z_][\w-]*:\s*$", line)
        and line.split(":")[0] not in AppConfig.model_fields
    ]
    assert not offenders, (
        f"bringup.sh emits top-level config keys AppConfig silently drops: "
        f"{offenders}"
    )
