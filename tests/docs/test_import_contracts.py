"""import-linter contracts may only name packages that still exist.

S9 section 4 makes the contract set part of the packaging sweep: a
forbidden_modules entry for a deleted package passes vacuously forever
and hides the day the rule stops being enforced.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _contracts() -> list[dict]:
    with (REPO / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["tool"]["importlinter"]["contracts"]


def _package_exists(dotted: str) -> bool:
    return (REPO / Path(*dotted.split("."))).exists() or (
        REPO / Path(*dotted.split(".")[:-1]) / f"{dotted.split('.')[-1]}.py"
    ).exists()


def test_contracts_name_only_live_modules() -> None:
    dead: list[str] = []
    for contract in _contracts():
        for key in ("source_modules", "forbidden_modules"):
            for dotted in contract.get(key, []):
                if not _package_exists(dotted):
                    dead.append(f"{contract['name']}: {key} -> {dotted}")
    assert not dead, f"contracts reference deleted packages: {dead}"


def test_ignore_imports_name_only_live_modules() -> None:
    dead: list[str] = []
    for contract in _contracts():
        for rule in contract.get("ignore_imports", []):
            for side in (s.strip() for s in rule.split("->")):
                if not _package_exists(side):
                    dead.append(f"{contract['name']}: {rule}")
    assert not dead, f"ignore_imports reference deleted modules: {dead}"


def test_chat_dispatcher_seam_is_gone() -> None:
    """S6 P5 deleted primer/channel/chat_dispatcher.py; its waiver goes too."""
    raw = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "chat_dispatcher" not in raw
