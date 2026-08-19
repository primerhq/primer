"""One extras matrix across every artifact that states it (S9 section 4).

pyproject, EXTRA_MODULES, the README install table and the modularity doc
each publish the same list. They drift silently; this makes them agree.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from primer.common.optional import EXTRA_MODULES

REPO = Path(__file__).resolve().parents[2]


def _first_column(text: str, after: str) -> list[str]:
    """First cell of every data row of the first markdown table after `after`."""
    tail = text.split(after, 1)[1]
    rows: list[str] = []
    started = False
    for line in tail.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            started = True
            cell = stripped.strip("|").split("|")[0].strip()
            rows.append(cell.strip("`").strip("*"))
        elif started:
            break
    # drop the header row and the |---|---| separator
    return [r for r in rows[2:] if r]


def _pyproject_extras() -> set[str]:
    with (REPO / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    return set(data["project"]["optional-dependencies"]) - {"full"}


def test_docling_extra_is_gone_everywhere() -> None:
    """S2 deleted the docling ingestion path; no artifact may still offer it."""
    assert "docling" not in _pyproject_extras()
    assert "docling" not in EXTRA_MODULES
    for rel in (
        "README.md",
        "docs/dev/subsystems/modularity.md",
        "Dockerfile",
        "docker-compose.yml",
        "AGENTS.md",
    ):
        body = (REPO / rel).read_text(encoding="utf-8")
        assert "docling" not in body.lower(), f"{rel} still mentions docling"


def test_extra_modules_matches_pyproject() -> None:
    assert set(EXTRA_MODULES) == _pyproject_extras()


def test_readme_install_table_matches_extra_modules() -> None:
    text = (REPO / "README.md").read_text(encoding="utf-8")
    rows = set(_first_column(text, "### Install options"))
    assert rows - {"(core)", "full"} == set(EXTRA_MODULES)


def test_modularity_doc_table_matches_extra_modules() -> None:
    text = (REPO / "docs" / "dev" / "subsystems" / "modularity.md").read_text(
        encoding="utf-8"
    )
    rows = set(_first_column(text, "which is the source of truth:"))
    assert rows == set(EXTRA_MODULES)


def test_channels_extra_survives_with_its_three_markers() -> None:
    """S6 keeps the channels extra and its module set unchanged."""
    assert EXTRA_MODULES["channels"] == ("slack_bolt", "telegram", "discord")


def test_speech_added_no_extra() -> None:
    """S4 decision: speech providers add NO packaging extra."""
    assert "speech" not in EXTRA_MODULES
    assert "speech" not in _pyproject_extras()
